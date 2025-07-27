from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import requests
from langchain_chroma import Chroma
from langchain.embeddings.base import Embeddings
from langchain.schema import Document
from typing import List
import hashlib
import json
import redis
from datetime import datetime

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse('static/index.html')

class NomicEmbedding(Embeddings):
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        response = requests.post(
            "http://127.0.0.1:1234/v1/embeddings",
            headers={"Content-Type": "application/json"},
            json={"model": "text-embedding-nomic-embed-text-v1.5", "input": texts}
        )
        return [d["embedding"] for d in response.json()["data"]]

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]

embedding = NomicEmbedding()
vectorstore = Chroma(persist_directory="./chroma_data", embedding_function=embedding)

# Initialize semantic cache (separate Chroma instance)
semantic_cache = Chroma(
    persist_directory="./semantic_cache_data", 
    embedding_function=embedding,
    collection_name="rag_responses"
)

# Initialize document cache
try:
    redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
    redis_client.ping()  # Test connection
    print("✅ Redis connected for document caching")
except:
    print("⚠️  Redis not available, document cache disabled")
    redis_client = None

# Configuration
SEMANTIC_SIMILARITY_THRESHOLD = 0.8  # Much higher threshold - identical questions scoring 0.655!
DOCUMENT_CACHE_TTL = 3600  # 1 hour
SEMANTIC_CACHE_TTL = 7 * 24 * 3600  # 7 days

@app.post("/query")
async def query_rag(request: Request):
    body = await request.json()
    question = body.get("question")
    
    print(f"\n🔍 Processing query: '{question}'")
    print(f"📊 Cache threshold: {SEMANTIC_SIMILARITY_THRESHOLD}")
    
    # === SEMANTIC CACHE CHECK ===
    print("🧠 Checking semantic cache...")
    try:
        cache_results = semantic_cache.similarity_search_with_score(question, k=3)  # Get top 3 for debugging
        print(f"📦 Found {len(cache_results)} cached items")
        
        if cache_results:
            for i, (doc, score) in enumerate(cache_results):
                cached_question = doc.metadata.get('question', 'N/A')
                print(f"   #{i+1}: Score {score:.3f} - Question: {cached_question[:50]}...")
                
                # Debug: Check if this is an exact match
                if cached_question.lower().strip() == question.lower().strip():
                    print(f"   🎯 EXACT TEXT MATCH found but similarity score is {score:.3f} (should be ~0.0)")
                    print(f"   💡 This suggests embeddings/ChromaDB issue - forcing cache hit!")
                    
                    # Force cache hit for exact text matches regardless of similarity score
                    try:
                        cached_response = json.loads(doc.page_content)
                        cached_response["cache_hit"] = True
                        cached_response["cache_type"] = "exact_match"
                        cached_response["similarity_score"] = score
                        cached_response["cached_question"] = cached_question
                        
                        print(f"🚀 FORCED cache hit for exact match!")
                        return cached_response
                    except json.JSONDecodeError as json_error:
                        print(f"❌ Failed to parse exact match cached response: {json_error}")
            
            # Check best match
            best_doc, best_score = cache_results[0]
            print(f"🎯 Best match score: {best_score:.3f} (threshold: {SEMANTIC_SIMILARITY_THRESHOLD})")
            print(f"🔍 Similarity check: {best_score:.3f} < {SEMANTIC_SIMILARITY_THRESHOLD} = {best_score < SEMANTIC_SIMILARITY_THRESHOLD}")
            
            if best_score < SEMANTIC_SIMILARITY_THRESHOLD:
                print(f"✅ SEMANTIC CACHE HIT! Using cached response (similarity: {best_score:.3f})")
                print(f"🚀 Cache hit means NO LLM processing needed - instant response!")
                
                # Parse cached response
                try:
                    cached_response = json.loads(best_doc.page_content)
                    cached_response["cache_hit"] = True
                    cached_response["cache_type"] = "semantic"
                    cached_response["similarity_score"] = best_score
                    cached_response["cached_question"] = best_doc.metadata.get('question', '')
                    
                    print(f"🚀 Returning cached response in ~50ms instead of 10+ seconds!")
                    return cached_response
                except json.JSONDecodeError as json_error:
                    print(f"❌ Failed to parse cached response JSON: {json_error}")
            else:
                print(f"❌ No cache hit - best score {best_score:.3f} > threshold {SEMANTIC_SIMILARITY_THRESHOLD}")
                print(f"💡 Identical questions should score ~0.0, not {best_score:.3f}")
                print(f"💡 Consider increasing threshold or checking embedding consistency")
        else:
            print("📭 No cached items found - this is a new question")
            
    except Exception as e:
        print(f"⚠️ Semantic cache error: {e}")
        import traceback
        traceback.print_exc()
    
    print("🔄 Cache miss - proceeding with full RAG pipeline...")
    
    # === DOCUMENT CACHE CHECK ===
    doc_cache_key = f"docs:{hashlib.md5(question.encode()).hexdigest()}"
    relevant_docs = None
    
    print(f"📄 Checking document cache with key: {doc_cache_key[:20]}...")
    if redis_client:
        try:
            cached_docs_json = redis_client.get(doc_cache_key)
            if cached_docs_json:
                print("📄 Document cache HIT!")
                cached_docs_data = json.loads(cached_docs_json)
                relevant_docs = [
                    type('Document', (), {
                        'page_content': doc['content'],
                        'metadata': doc['metadata']
                    })() 
                    for doc in cached_docs_data
                ]
                print(f"📄 Loaded {len(relevant_docs)} cached documents")
            else:
                print("📄 Document cache MISS")
        except Exception as e:
            print(f"⚠️ Document cache error: {e}")
    else:
        print("📄 Redis not available - document cache disabled")
    
    # === FULL VECTOR SEARCH (if no document cache hit) ===
    if relevant_docs is None:
        print("🔍 Performing full vector search...")
        results = vectorstore.similarity_search_with_score(question, k=10)
        relevant_docs = [doc for doc, score in results if score < 1]
        
        # Cache the documents
        if redis_client and relevant_docs:
            try:
                docs_to_cache = [
                    {
                        'content': doc.page_content,
                        'metadata': doc.metadata
                    }
                    for doc in relevant_docs
                ]
                redis_client.setex(
                    doc_cache_key, 
                    DOCUMENT_CACHE_TTL, 
                    json.dumps(docs_to_cache)
                )
                print(f"📄 Cached {len(relevant_docs)} documents")
            except Exception as e:
                print(f"⚠️ Document cache save error: {e}")
    
    if not relevant_docs:
        return {
            "answer": "I couldn't find relevant information in the ArcGIS Enterprise documentation.",
            "reasoning": "No relevant documents found in the knowledge base.",
            "raw_response": "",
            "used_granite": False,
            "cache_hit": False
        }
    
    # === LLM PROCESSING ===
    context = "\n\n".join([doc.page_content for doc in relevant_docs])
    
    try:
        print(f"Received question: {question}")
        
        # Step 1: Get detailed response with reasoning from DeepSeek-R1
        deepseek_payload = {
            "model": "deepseek-r1-distill-qwen-7b",
            "messages": [
                {"role": "system", "content": "You are an ArcGIS Enterprise expert. Think through the problem step by step and provide a detailed technical answer based on the context."},
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}
            ],
            "temperature": 0.7,
            "max_tokens": -1,
            "stream": False
        }

        print("Step 1: Sending request to DeepSeek-R1...")
        deepseek_response = requests.post(
            "http://127.0.0.1:1234/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json=deepseek_payload,
            timeout=60
        )
        
        if deepseek_response.status_code != 200:
            print(f"DeepSeek error: {deepseek_response.status_code} - {deepseek_response.text}")
            return {"error": f"DeepSeek error: {deepseek_response.status_code}"}

        deepseek_result = deepseek_response.json()
        raw_answer = deepseek_result['choices'][0]['message']['content']
        print(f"DeepSeek response length: {len(raw_answer)} characters")
        
        # Extract reasoning and final answer
        reasoning = ""
        final_answer = raw_answer
        use_granite = False
        
        # Look for thinking tags in the response
        if "<think>" in raw_answer and "</think>" in raw_answer:
            print("Found <think> tags - extracting reasoning...")
            think_start = raw_answer.find("<think>")
            think_end = raw_answer.find("</think>")
            reasoning = raw_answer[think_start+7:think_end].strip()
            final_answer = raw_answer[think_end+8:].strip()
            print(f"Extracted reasoning ({len(reasoning)} chars) and answer ({len(final_answer)} chars)")
            
            # Check if final answer is well-formatted
            if not any(marker in final_answer for marker in ["##", "**", "-", "*", "`", "1."]):
                print("Final answer lacks formatting - will use Granite for cleanup")
                use_granite = True
        else:
            print("No <think> tags found - will use Granite for formatting")
            use_granite = True

        # Step 2: Only use Granite if the answer needs formatting cleanup
        if final_answer.strip() and use_granite:
            print("Step 2: Sending to Granite for formatting cleanup...")
            granite_payload = {
                "model": "granite-3.1-8b-instruct:2",
                "messages": [
                    {
                        "role": "system", 
                        "content": "You are a technical documentation specialist. Take the provided response and create a clean, well-formatted markdown summary. Use headings, bullet points, code blocks, and clear structure. Be concise but comprehensive."
                    },
                    {
                        "role": "user", 
                        "content": f"Please format this ArcGIS Enterprise response into clean markdown:\n\n{final_answer}"
                    }
                ],
                "temperature": 0.3,
                "max_tokens": 1024,
                "stream": False
            }
            
            granite_response = requests.post(
                "http://127.0.0.1:1234/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                json=granite_payload,
                timeout=60
            )
            
            if granite_response.status_code == 200:
                granite_result = granite_response.json()
                summarized_answer = granite_result['choices'][0]['message']['content']
                print(f"Granite formatted response length: {len(summarized_answer)} characters")
                
                # === CACHE THE GRANITE RESPONSE ===
                print("💾 Caching Granite-formatted response...")
                response_to_cache = {
                    "answer": summarized_answer,
                    "reasoning": reasoning if reasoning else None,
                    "raw_response": final_answer,
                    "used_granite": True,
                    "cache_hit": False,
                    "cached_at": datetime.now().isoformat()
                }
                
                # Store in semantic cache
                try:
                    cache_document = Document(
                        page_content=json.dumps(response_to_cache),
                        metadata={
                            "question": question,
                            "timestamp": datetime.now().isoformat(),
                            "doc_count": len(relevant_docs),
                            "response_type": "granite"
                        }
                    )
                    semantic_cache.add_documents([cache_document])
                    print(f"✅ Successfully cached Granite response for question: '{question[:50]}...'")
                    print(f"📊 Cache now contains response for future similar queries")
                except Exception as cache_error:
                    print(f"❌ Failed to cache Granite response: {cache_error}")
                    import traceback
                    traceback.print_exc()
                
                return {
                    "answer": summarized_answer,
                    "reasoning": reasoning if reasoning else None,
                    "raw_response": final_answer,
                    "used_granite": True,
                    "cache_hit": False
                }
            else:
                print(f"Granite error, using DeepSeek response: {granite_response.status_code}")
        
        # === CACHE THE DEEPSEEK RESPONSE (when Granite not used or failed) ===
        print("💾 Caching DeepSeek response...")
        response_to_cache = {
            "answer": final_answer,
            "reasoning": reasoning if reasoning else None,
            "raw_response": raw_answer,
            "used_granite": False,
            "cache_hit": False,
            "cached_at": datetime.now().isoformat()
        }
        
        # Store in semantic cache
        try:
            cache_document = Document(
                page_content=json.dumps(response_to_cache),
                metadata={
                    "question": question,
                    "timestamp": datetime.now().isoformat(),
                    "doc_count": len(relevant_docs),
                    "response_type": "deepseek"
                }
            )
            semantic_cache.add_documents([cache_document])
            print(f"✅ Successfully cached DeepSeek response for question: '{question[:50]}...'")
            print(f"📊 Cache now contains response for future similar queries")
        except Exception as cache_error:
            print(f"❌ Failed to cache DeepSeek response: {cache_error}")
            import traceback
            traceback.print_exc()
        
        # Use DeepSeek response directly
        return {
            "answer": final_answer,
            "reasoning": reasoning if reasoning else None,
            "raw_response": raw_answer,
            "used_granite": False,
            "cache_hit": False
        }
        
    except Exception as e:
        print(f"❌ Error in query_rag: {str(e)}")
        import traceback
        traceback.print_exc()
        return {"error": f"Server error: {str(e)}"}

# Add a debug endpoint to check cache contents
@app.get("/debug/cache")
async def debug_cache():
    try:
        # Get all cached items
        cache_results = semantic_cache.similarity_search("", k=10)
        cache_info = []
        
        for doc in cache_results:
            try:
                cached_data = json.loads(doc.page_content)
                cache_info.append({
                    "question": doc.metadata.get('question', 'N/A')[:100],
                    "timestamp": doc.metadata.get('timestamp', 'N/A'),
                    "response_type": doc.metadata.get('response_type', 'unknown'),
                    "answer_preview": cached_data.get('answer', 'N/A')[:100] + "..." if cached_data.get('answer') else 'N/A'
                })
            except:
                cache_info.append({
                    "question": doc.metadata.get('question', 'N/A'),
                    "error": "Failed to parse cached response"
                })
        
        return {
            "cache_count": len(cache_info),
            "cached_items": cache_info,
            "threshold": SEMANTIC_SIMILARITY_THRESHOLD
        }
    except Exception as e:
        return {"error": f"Failed to debug cache: {str(e)}"}
