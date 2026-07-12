FROM python:3.10-slim

WORKDIR /app

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    fastapi \
    uvicorn[standard] \
    streamlit \
    langchain \
    langchain-community \
    langchain-core \
    chromadb \
    pymysql \
    mysql-connector-python \
    redis \
    dashscope \
    PyPDF2 \
    python-multipart \
    pyyaml

RUN pip install --no-cache-dir --timeout 600 -i https://mirrors.aliyun.com/pypi/simple/ \
    sentence-transformers

COPY . .

EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]