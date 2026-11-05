FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask gunicorn
COPY . /app
ENV DB_PATH=/data/etsy.db
EXPOSE 5055
CMD ["gunicorn","-b","0.0.0.0:5055","-w","1","--threads","4","--timeout","600","app:app"]
