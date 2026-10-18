FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT} -w 1 --threads 8 --timeout 120 app:app"]
