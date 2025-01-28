#!/usr/bin/env python3

import os
from PIL import Image
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Image configurations
IMAGE_CONFIGS = {
    'banners': [
        {'suffix': '772x250', 'size': (772, 250)},
        {'suffix': '1544x500', 'size': (1544, 500)},
    ],
    'icons': [
        {'suffix': '256', 'size': (256, 256)},
        {'suffix': '1024', 'size': (1024, 1024)},
    ]
}

def resize_image(image, target_size):
    """
    Resize image to cover target size while maintaining aspect ratio.
    Similar to CSS object-fit: cover
    """
    target_width, target_height = target_size
    original_width, original_height = image.size

    # Calculate aspect ratios
    target_aspect = target_width / target_height
    original_aspect = original_width / original_height

    # Calculate dimensions to maintain aspect ratio while covering
    if original_aspect > target_aspect:
        # Original image is wider - scale to match height
        new_height = target_height
        new_width = int(new_height * original_aspect)
    else:
        # Original image is taller - scale to match width
        new_width = target_width
        new_height = int(new_width / original_aspect)

    # Resize image
    resized = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

    # Create new image with target size
    result = Image.new('RGB', target_size)

    # Calculate position to center the crop
    left = (new_width - target_width) // 2
    top = (new_height - target_height) // 2

    # Crop and paste into center
    crop_box = (left, top, left + target_width, top + target_height)
    cropped = resized.crop((
        max(0, left),
        max(0, top),
        min(new_width, left + target_width),
        min(new_height, top + target_height)
    ))
    paste_position = (
        max(0, -left),
        max(0, -top)
    )
    result.paste(cropped, paste_position)

    return result

def process_image(source_path):
    """Process image and create all required sizes."""
    try:
        # Open source image
        with Image.open(source_path) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'LA'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            output_dir = os.path.dirname(source_path)

            # Process banners
            for config in IMAGE_CONFIGS['banners']:
                output_path = os.path.join(output_dir, f"banner-{config['suffix']}.webp")
                resized = resize_image(img.copy(), config['size'])
                resized.save(output_path, 'WEBP', quality=85)
                logger.info(f"Created banner: {output_path}")

            # Process icons
            for config in IMAGE_CONFIGS['icons']:
                output_path = os.path.join(output_dir, f"icon-{config['suffix']}.webp")
                resized = resize_image(img.copy(), config['size'])
                resized.save(output_path, 'WEBP', quality=85)
                logger.info(f"Created icon: {output_path}")

    except Exception as e:
        logger.error(f"Error processing image: {str(e)}")
        raise

def main():
    """Main function to run the image processing."""
    try:
        # Get the project root directory
        project_root = Path(__file__).parent.parent
        source_image = project_root / 'assets' / 'images' / 'placeholder.webp'

        if not source_image.exists():
            raise FileNotFoundError(f"Source image not found: {source_image}")

        logger.info(f"Starting image processing from: {source_image}")
        process_image(str(source_image))
        logger.info("Image processing completed successfully!")

    except Exception as e:
        logger.error(f"Error in main execution: {str(e)}")
        raise

if __name__ == "__main__":
    main()
