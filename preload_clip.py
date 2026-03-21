import os
os.environ["TORCH_HOME"] = "/app/.cache/torch"
import clip
print("Téléchargement modèle CLIP ViT-B/32...")
model, preprocess = clip.load("ViT-B/32", device="cpu", download_root="/app/.cache/clip")
print("Modèle CLIP OK")
