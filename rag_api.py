from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import requests
from langchain_chroma import Chroma
from langchain.embeddings.base import Embeddings
from langchain.schema import Document
from typing import List

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

@app.post("/query")
async def query_rag(request: Request):
    try:
        body = await request.json()
        question = body.get("question")
        
        if not question:
            return {"error": "No question provided"}

        print(f"Received question: {question}")
        
        results = vectorstore.similarity_search_with_score(question, k=4)
        relevant_docs = [doc for doc, score in results if score < 0.8]

        if not relevant_docs:
            return {
                "answer": "Sorry, I couldn't find any relevant documentation for that. Please ask something related to ArcGIS Enterprise."
            }

        context = "\n\n".join([doc.page_content for doc in relevant_docs])
        
        print(f"Found {len(relevant_docs)} relevant documents out of {len(results)} (similarity threshold: 0.8)")

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
        # payload = {
        #     "model": "phi-3-mini-4k-instruct", # "phi-4-mini-reasoning-mlx",  # Choose a non-reasoning version (see Part 3)
        #     "messages": [
        #         {
        #             "role": "system",
        #             "content": (
        #                 "You are an ArcGIS Enterprise technical assistant. Answer questions about ArcGIS Enterprise setup, configuration, deployment, and troubleshooting.\n\n"
        #                 "Guidelines:\n"
        #                 "- Use the provided context to answer questions\n"
        #                 "- Format your response in markdown\n"
        #                 "- Be concise and technical\n"
        #                 "- If the question is not about ArcGIS Enterprise, respond: 'I can only help with ArcGIS Enterprise questions.'\n"
        #                 "- Structure your answer with headings, bullet points, and code blocks as needed"
        #             )
        #         },
        #         {
        #             "role": "user",
        #             "content": f"Based on this documentation:\n\n{context}\n\nAnswer this question: {question}"
        #         }
        #     ],
        #     "temperature": 0.1,
        #     "max_tokens": 512,
        #     "stream": False
        # }


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
        use_granite = False  # Flag to determine if we need Granite processing
        
        # Look for thinking tags in the response
        if "<think>" in raw_answer and "</think>" in raw_answer:
            print("Found <think> tags - extracting reasoning...")
            think_start = raw_answer.find("<think>")
            think_end = raw_answer.find("</think>")
            reasoning = raw_answer[think_start+7:think_end].strip()
            final_answer = raw_answer[think_end+8:].strip()
            print(f"Extracted reasoning ({len(reasoning)} chars) and answer ({len(final_answer)} chars)")
            
            # Check if final answer is well-formatted (has markdown elements)
            if not any(marker in final_answer for marker in ["##", "**", "-", "*", "`", "1."]):
                print("Final answer lacks formatting - will use Granite for cleanup")
                use_granite = True
        else:
            print("No <think> tags found - checking for alternative reasoning patterns...")
            # Alternative format detection
            lines = raw_answer.split('\n')
            reasoning_lines = []
            answer_lines = []
            in_reasoning = False
            
            for line in lines:
                if any(marker in line.lower() for marker in ["reasoning:", "analysis:", "thinking:", "let me think"]):
                    in_reasoning = True
                    reasoning_lines.append(line)
                    continue
                elif any(marker in line.lower() for marker in ["answer:", "solution:", "conclusion:", "result:"]):
                    in_reasoning = False
                    answer_lines.append(line)
                    continue
                
                if in_reasoning:
                    reasoning_lines.append(line)
                else:
                    answer_lines.append(line)
            
            if reasoning_lines:
                reasoning = '\n'.join(reasoning_lines).strip()
                final_answer = '\n'.join(answer_lines).strip()
                print(f"Extracted reasoning using pattern matching ({len(reasoning)} chars)")
                use_granite = True  # Alternative format likely needs cleanup
            else:
                print("No reasoning pattern found - treating entire response as answer")
                use_granite = True  # No separation means response might need formatting

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
                
                return {
                    "answer": summarized_answer,
                    "reasoning": reasoning if reasoning else None,
                    "raw_response": final_answer,
                    "used_granite": True
                }
            else:
                print(f"Granite error, using DeepSeek response: {granite_response.status_code}")
        else:
            print("DeepSeek response is well-formatted - skipping Granite processing")
        
        # Use DeepSeek response directly (either because it's well-formatted or Granite failed)
        return {
            "answer": final_answer,
            "reasoning": reasoning if reasoning else None,
            "raw_response": raw_answer,
            "used_granite": False
        }
        
    except Exception as e:
        print(f"Error in query_rag: {str(e)}")
        return {"error": f"Server error: {str(e)}"}
