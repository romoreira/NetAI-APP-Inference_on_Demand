FROM python:3.8
WORKDIR /app
COPY client_fed_inf.py requirements.txt ./
RUN pip install -r requirements.txt
CMD ["python", "client_fed_inf.py"]
