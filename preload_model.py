import torchvision.models as models
print("Téléchargement MobileNetV2...")
model = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.IMAGENET1K_V1)
print("MobileNetV2 téléchargé et mis en cache OK")
