FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py router.py user_data_auth.py alerts_worker.py ./
CMD ["python", "alerts_worker.py"]
