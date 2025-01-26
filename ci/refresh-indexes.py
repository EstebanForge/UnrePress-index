import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any
from collections import Counter
import nltk
from nltk.tokenize import word_tokenize
from nltk.corpus import stopwords
from nltk.stem import WordNetLemmatizer

# Download required NLTK data
try:
    nltk.data.find('tokenizers/punkt')
    nltk.data.find('corpora/wordnet')
    nltk.data.find('corpora/stopwords')
except LookupError:
    nltk.download('punkt')
    nltk.download('wordnet')
    nltk.download('stopwords')

def extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from text using NLTK.

    Args:
        text: Input text to extract keywords from

    Returns:
        List of relevant keywords
    """
    # Initialize lemmatizer
    lemmatizer = WordNetLemmatizer()

    # Tokenize and convert to lowercase
    tokens = word_tokenize(text.lower())

    # Get English stop words
    stop_words = set(stopwords.words('english'))

    # Additional stop words and blacklist
    additional_stop_words = {
        'wordpress', 'plugin', 'theme', 'plugins', 'themes', 'site', 'sites', 'web',
        'website', 'websites', 'page', 'pages', 'post', 'posts', 'content', 'using',
        'use', 'used', 'uses', 'create', 'created', 'creates', 'creating', 'make',
        'makes', 'making', 'made', 'add', 'adds', 'adding', 'added', 'get', 'gets',
        'getting', 'got', 'set', 'sets', 'setting', 'settings'
    }
    stop_words.update(additional_stop_words)

    # Process tokens
    keywords = []
    for word in tokens:
        if (word not in stop_words and  # Not a stop word
            len(word) > 2 and  # Longer than 2 characters
            word.isalnum()):  # Contains only letters and numbers
            lemma = lemmatizer.lemmatize(word)
            keywords.append(lemma)

    # Count word frequency
    word_freq = Counter(keywords)

    # Get unique words, prioritizing by frequency
    unique_words = sorted(set(keywords), key=lambda x: (-word_freq[x], x))

    return unique_words[:10]  # Limit to top 10 most relevant keywords

def generate_index(index_dir: str, source_dir: str, output_file: str) -> None:
    """Generate an index file for plugins or themes.

    Args:
        index_dir: Root directory of the repository
        source_dir: Directory name ('plugins' or 'themes')
        output_file: Path to output index file
    """
    items = []
    source_path = os.path.join(index_dir, source_dir)

    # Traverse directory and its subdirectories
    for root, _, files in os.walk(source_path):
        for file in files:
            if file.endswith(".json"):
                item_path = Path(root) / file
                with open(item_path, "r") as f:
                    try:
                        item_data = json.load(f)
                        name = item_data.get("name", "")
                        description = item_data.get("sections", {}).get("description", "")

                        # Extract keywords from name and description
                        keywords = extract_keywords(f"{name} {description}")

                        items.append({
                            "slug": item_data.get("slug", ""),
                            "name": name,
                            "description": description,
                            "tags": keywords
                        })
                    except json.JSONDecodeError:
                        print(f"Skipping invalid JSON: {item_path}")

    # Write to index file
    with open(output_file, "w") as f:
        json.dump({
            "schema_version": 1,
            "total": len(items),
            "plugins" if source_dir == "plugins" else "themes": items
        }, f, indent=2)

if __name__ == "__main__":
    base_dir = Path(__file__).parent.parent

    # Generate plugins index
    generate_index(
        index_dir=str(base_dir),
        source_dir="plugins",
        output_file=str(base_dir / "discovery" / "plugins-index.json")
    )

    # Generate themes index
    generate_index(
        index_dir=str(base_dir),
        source_dir="themes",
        output_file=str(base_dir / "discovery" / "themes-index.json")
    )
