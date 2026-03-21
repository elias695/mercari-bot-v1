import torch
print("Téléchargement DINOv2 ViT-L/14...")
model = torch.hub.load("facebookresearch/dinov2", "dinov2_vitl14")
print("DINOv2 téléchargé et mis en cache OK ✅")
