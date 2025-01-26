import nltk

def download_nltk_resources():
    """Download required NLTK resources for the refresh-indexes script."""
    resources = [
        'punkt',
        'punkt_tab',
        'wordnet',
        'stopwords'
    ]
    
    for resource in resources:
        print(f"Downloading {resource}...")
        nltk.download(resource)
        print(f"✓ {resource} downloaded successfully")

if __name__ == "__main__":
    download_nltk_resources()
