import os
import re
from pathlib import Path
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

def clean_markdown(text):
    # Strip YAML frontmatter
    text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.DOTALL)
    # Strip code blocks
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    # Strip markdown images entirely
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    # Strip markdown links but keep text: [text](link) -> text
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
    return text

def process_directory(directory_path):
    blocks = []
    metadata = []
    
    path = Path(directory_path)
    if not path.exists() or not path.is_dir():
        return blocks, metadata
        
    for md_file in path.rglob('*.md'):
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            cleaned_content = clean_markdown(content)
            paragraphs = cleaned_content.split('\n\n')
            
            for para in paragraphs:
                para = para.strip()
                word_count = len(para.split())
                if word_count >= 5:
                    blocks.append(para)
                    metadata.append({
                        'file': md_file.name,
                        'word_count': word_count
                    })
        except Exception as e:
            # We ignore specific file read errors in the engine, allowing it to process the rest
            pass
            
    return blocks, metadata

def generate_context_packet(target_dir, query, top_k=5):
    """
    Scans the directory, calculates TF-IDF against the query, 
    and returns a tuple of (xml_string, list_of_metadata_dicts, error_message).
    """
    if not target_dir or not query:
        return "", [], "Target directory and query must be provided."
        
    blocks, metadata = process_directory(target_dir)
    
    if not blocks:
        return "", [], "Directory not found, empty, or contains no valid markdown blocks."
        
    vectorizer = TfidfVectorizer(stop_words='english')
    try:
        tfidf_matrix = vectorizer.fit_transform(blocks)
    except ValueError:
        return "", [], "Not enough valid text data to build vocabulary."
        
    query_vec = vectorizer.transform([query])
    similarities = cosine_similarity(query_vec, tfidf_matrix).flatten()
    
    if np.max(similarities) == 0:
        return "", [], "No relevant context found for your query."
        
    top_indices = similarities.argsort()[-top_k:][::-1]
    
    packet_xml = "<context_packet>\n"
    results_meta = []
    
    for idx in top_indices:
        score = similarities[idx]
        if score == 0:
            continue
            
        block_text = blocks[idx]
        meta = metadata[idx]
        word_count = meta['word_count']
        economy_metric = (score * 1000) / word_count if word_count > 0 else 0
        
        results_meta.append({
            'file': meta['file'],
            'word_count': word_count,
            'economy_metric': economy_metric,
            'text': block_text,
            'score': score
        })
        
        packet_xml += f"  <context_block source=\"{meta['file']}\">\n"
        packet_xml += f"    {block_text}\n"
        packet_xml += f"  </context_block>\n"
        
    packet_xml += "</context_packet>"
    
    return packet_xml, results_meta, None
