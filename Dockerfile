FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/b11011/naas-sim \
      org.opencontainers.image.description="Stateful simulator of Lumen's NaaS bandwidth-on-demand APIs" \
      org.opencontainers.image.licenses=MIT

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simulator/ simulator/

# Containers need to listen on all interfaces to be reachable via port mapping
ENV NAAS_SIM_HOST=0.0.0.0 \
    NAAS_SIM_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "simulator"]
