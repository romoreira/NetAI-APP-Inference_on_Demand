import requests

# Abre a imagem em modo binário
imagem = open("0.jpg", "rb")

# Define a URL e os parâmetros da requisição
url = "http://172.210.35.177:5001/predict"
parametros = {"chave": "valor"}

# Envia a imagem como parte dos dados do formulário, com o nome de campo correto
resposta = requests.post(url, data=parametros, files={"file": imagem})
print(str(resposta.text))
# Fecha o arquivo da imagem
imagem.close()

# Verifica o status da resposta
if resposta.status_code == 200:
    print("Imagem enviada com sucesso!")
else:
    print("Ocorreu um erro ao enviar a imagem.")