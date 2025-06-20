import asyncio,sys
# import nest_asyncio 
import streamlit as st
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from duckduckgo_search import DDGS
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.content_filter_strategy import BM25ContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from crawl4ai.models import CrawlResult
import chromadb, tempfile, ollama
from chromadb.config import Settings
from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
from langchain_community.document_loaders import UnstructuredMarkdownLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
# nest_asyncio.apply()

system_prompt = """
You are an AI assistant tasked with providing detailed answers based solely on the given context.
Your goal is to analyze the information provided and formulate a comprehensive, well-structured response to the question.

Context will be passed as "Context:"
User question will be passed as "Question:"

To answer the question:
1. Thoroughly analyze the context, identifying key information relevant to the question.
2. Organize your thoughts and plan your response to ensure a logical flow of information.
3. Formulate a detailed answer that directly addresses the question, using only the information provided in the context.
4. When the context supports an answer, ensure your response is clear, concise, and directly addresses the question.
5. When there is no context, just say you have no context and stop immediately.
6. If the context doesn't contain sufficient information to fully answer the question, state this clearly in your response.
7. Avoid explaining why you cannot answer or speculating about missing details. Simply state that you lack sufficient context when necessary.

Format your response as follows:
1. Use clear, concise language.
2. Organize your answer into paragraphs for readability.
3. Use bullet points or numbered lists where appropriate to break down complex information.
4. If relevant, include any headings or subheadings to structure your response.
5. Ensure proper grammar, punctuation, and spelling throughout your answer.
6. Do not mention what you received in context, just focus on answering based on the context.

Important: Base your entire response solely on the information provided in the context. Do not include any external knowledge or assumptions not present in the given text.
"""

def call_llm(prompt:str, with_context:bool=True,context:str |None=None):
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role":"user",
            "content":f"Context: {context}, Question: {prompt} ",
        }]
    
    if not with_context:
        messages.pop(0),
        messages[0]["content"] = prompt
        
    response = ollama.chat(model="llama3.2:3b", stream=True, messages=messages)
    
    for chunk in response:
        if chunk["done"] is False:
            yield chunk["message"]["content"]
        else:
            break

def normalized_url(url):
    normalized_url = (url.replace("https://","").replace("www.","").replace("/","_").replace("-","_").replace(".","_"))
    return normalized_url

def get_vector_collection() -> tuple[chromadb.Collection ,chromadb.Client]:
    ollama_ef = OllamaEmbeddingFunction(
        url = "https://localhost:11434/api/embeddings",
        model_name="nomic-embed-text:latest",
    )
    
    chroma_client = chromadb.PersistentClient(
        path="./seekify-db", settings=Settings(anonymized_telemetry=False)
    )
    
    return (
        chroma_client.get_or_create_collection(
            name="seekify",
            embedding_function=ollama_ef,
            metadata={"hnsw:space":"cosine"},
        ),
        chroma_client
    )
    
def add_to_vector_database(results: list[CrawlResult]):
    collection, _ = get_vector_collection()
    
    for result in results:
        # st.write(result)
        documents, metadatas, ids = [], [], []
        
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size = 400,
            chunk_overlap=100,
            separators=["\n\n", "\n", ".", "?", "!", " ", ""],
        )
        
        if result.markdown:
            markdown_result = result.markdown.fit_markdown
        else:
            continue
        
        temp_file = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
        temp_file.write(markdown_result)
        temp_file.flush()
        
        loader = UnstructuredMarkdownLoader(
            temp_file.name, 
            mode="single",
            unstructured_kwargs={"encoding": "windows-1252"})
        docs = loader.load()
        all_splits = text_splitter.split_documents(docs)
        st.write(all_splits)
        normalize_url = normalized_url(result.url)
        
        if all_splits:
            for idx, split in enumerate(all_splits):
                documents.append(split.page_content)
                metadatas.append({"source":result.url})
                ids.append(f"{normalized_url}_{idx}")
            
            collection.upsert(
                documents=documents,metadatas=metadatas, ids=ids
            )
    
# Function of crawler for searching multiple urls at the same time
async def crawl_webpages(urls: list[str], prompt: str) -> CrawlResult:
    bm25_filter = BM25ContentFilter(user_query=prompt, bm25_threshold=1.2)
    md_generator = DefaultMarkdownGenerator(content_filter=bm25_filter)
    
    crawler_config = CrawlerRunConfig(
        markdown_generator=md_generator,
        excluded_tags=["nav", "footer", "header", "form", "img", "a"],
        only_text=True,
        exclude_social_media_links=True,
        cache_mode=CacheMode.BYPASS,
        remove_overlay_elements=True,
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        page_timeout=2000, # in ms: 20 in Seconds
    )
    
    browser_config = BrowserConfig(headless=True, text_mode=True, light_mode=True)
    
    async with AsyncWebCrawler(config=browser_config) as crawler:
      results = await crawler.arun_many(urls, config=crawler_config)
      return results
    

# Function to filter the searches results and urls
def check_robots_txt(urls: list[str]) -> list[str]:
    allowerd_urls = []
    
    for url in urls:
        try:
            robots_urls = f"{urlparse(url).scheme}://{urlparse(url).netloc}/robots.txt"
            rp = RobotFileParser(robots_urls)
            rp.read()
            
            if rp.can_fetch("*", url):
                allowerd_urls.append(url)
        except Exception:
            # If robots.txt is missing or there's any error, assume URL is allowed
            allowerd_urls.append(url)
    
    return allowerd_urls

# Function to get the results and related urls
def get_web_urls(search_term: str, num_results: int= 10) -> list[str]:
    try:
        discard_urls = ["youtube.com", "britannica.com", "vimeo.com"]
        for url in discard_urls:
            search_term += f" -site:{url}"
        
        results = DDGS().text(search_term, max_results=num_results)
        results = [result["href"] for result in results]
        
        # st.write(results)
        return check_robots_txt(results)
    
    except Exception as e:
        errror_msg = ("Failed to fetch the results",str(e))
        print(errror_msg)
        st.write(errror_msg)
        st.stop()
    

# Main function including the UI of the website 
async def run():
    st.set_page_config(page_title="Seekify")
    
    st.header("Seekify")
    st.subheader("Seek your answers")
    prompt = st.text_area(
        label="Enter your query here",
        placeholder="Add your query",
        label_visibility="hidden",
    )
    
    is_web_search = st.toggle("Enable Web Search", value=False,key="enable_web_search")
    go = st.button("Search")
    
    collection, chroma_client = get_vector_collection()
    
    if prompt and go:
        if is_web_search:
            web_urls = get_web_urls(search_term=prompt)
            if not web_urls:
                st.write("No results found.")
                st.stop()
            
            results = await crawl_webpages(urls=web_urls,prompt=prompt)
            add_to_vector_database(results)
            
            qresults = collection.query(query_texts=[prompt], n_results=10)
            context = qresults.get("documents")[0]
            
            chroma_client.delete_collection(
                name="seekify"
            )
            
            llm_response = call_llm(
                context = context, prompt=prompt, with_context=is_web_search
            )
            st.write_stream(llm_response)
            
        else:
             qresults = collection.query(query_texts=[prompt], n_results=10)
             context = qresults.get("documents")[0]
            
             llm_response = call_llm(context=context, prompt=prompt, with_context=is_web_search)
             st.write_stream(llm_response)
    
if __name__ == "__main__":
    asyncio.run(run())