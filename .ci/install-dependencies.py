import nltk
import os
import subprocess
import sys
from pathlib import Path

def install_pip_packages():
    """Install required pip packages."""
    packages = [
        'Pillow>=10.0.0',  # For image processing
        'nltk>=3.8.1'      # For text processing
    ]

    print("Installing pip packages...")
    for package in packages:
        print(f"Installing {package}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                             "--user", "--upgrade", package])
        print(f"✓ {package} installed successfully")

def download_nltk_resources():
    """Download required NLTK resources for the refresh-indexes script."""
    # Set NLTK data path to ~/.config/pip/nltk_data
    nltk_data_dir = os.path.expanduser("~/.config/pip/nltk_data")

    # Create the directory if it doesn't exist
    Path(nltk_data_dir).mkdir(parents=True, exist_ok=True)

    # Set the NLTK data path
    nltk.data.path.insert(0, nltk_data_dir)

    resources = [
        'punkt',
        'punkt_tab',
        'wordnet',
        'stopwords'
    ]

    print("\nDownloading NLTK resources...")
    for resource in resources:
        print(f"Downloading {resource}...")
        nltk.download(resource, download_dir=nltk_data_dir)
        print(f"✓ {resource} downloaded successfully")

if __name__ == "__main__":
    install_pip_packages()
    download_nltk_resources()
    print("\n✓ All dependencies installed successfully!")
