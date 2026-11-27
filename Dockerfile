FROM python:3.11-slim
WORKDIR /srv/kilnledger
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KILN_DATA=/store KILN_PORT=8080 OPENBLAS_NUM_THREADS=1
COPY requirements.lock .
COPY .wheelhouse /opt/kiln-wheels
RUN if [ "$(find /opt/kiln-wheels -name '*.whl' -print -quit)" ]; then \
      python -m pip install --no-index --find-links=/opt/kiln-wheels -r requirements.lock; \
    else python -m pip install --no-cache-dir -r requirements.lock; fi
COPY kiln kiln
COPY static static
COPY demo demo
EXPOSE 8080
VOLUME ["/store"]
CMD ["python", "-m", "kiln.app"]
