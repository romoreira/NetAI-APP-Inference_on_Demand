import io
import json
import os
from torchvision import models
import torchvision.transforms as transforms
from PIL import Image
from flask import Flask, jsonify, request
import torch.nn.functional as F

# Obtendo o número da porta da variável de ambiente
port = int(os.getenv("PORT_NUMBER", 5000))


app = Flask(__name__)
imagenet_class_index = json.load(open('imagenet_class_index.json'))
model = models.densenet121(weights='IMAGENET1K_V1')
model.eval()


def transform_image(image_bytes):
    my_transforms = transforms.Compose([transforms.Resize(255),
                                        transforms.CenterCrop(224),
                                        transforms.ToTensor(),
                                        transforms.Normalize(
                                            [0.485, 0.456, 0.406],
                                            [0.229, 0.224, 0.225])])
    image = Image.open(io.BytesIO(image_bytes))
    return my_transforms(image).unsqueeze(0)


def get_prediction(image_bytes):
    tensor = transform_image(image_bytes=image_bytes)
    outputs = model.forward(tensor)
    probabilities = F.softmax(outputs, dim=1)
    predicted_probabilities, predicted_indices = probabilities.max(1)
    predicted_idx = predicted_indices.item()
    class_name = imagenet_class_index[str(predicted_idx)]  # Obtém o nome da classe ou "Unknown" se não encontrado
    return predicted_idx, class_name, predicted_probabilities.item()

@app.route('/predict', methods=['POST'])
def predict():
    if request.method == 'POST':
        if 'file' not in request.files:
            return jsonify({'error': 'No file part in the request'}), 400
        file = request.files['file']
        img_bytes = file.read()
        class_id, class_name, probabilities = get_prediction(image_bytes=img_bytes)
        return jsonify({'class_id': class_id, 'class_name': class_name, 'probability': probabilities})


if __name__ == '__main__':
    app.run(app.run(host='0.0.0.0', port=port, debug=True))