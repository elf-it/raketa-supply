# Образ бэкенда (задача A-7).
#
# Миграции образ не применяет: схему разворачивает отдельный шаг развёртывания
# (в compose — служба `migrate`). Приложение на старте только проверяет ревизию
# и падает с понятным текстом, если база устарела, — работать на устаревшей базе
# хуже, чем не запуститься.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости ставятся отдельным слоем и до кода: иначе правка одной строки
# в приложении пересобирала бы весь слой с пакетами.
# requirements.txt, а не -dev: линтер и pytest на сервере не нужны.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Не от root: процесс в контейнере не должен уметь переписать собственный код.
RUN useradd --create-home --uid 10001 raketa && chown -R raketa:raketa /app
USER raketa

EXPOSE 8000

# Воркеров один: кэш расчёта живёт в памяти процесса, и каждый лишний воркер —
# ещё одна его копия (задача G-14, вопрос Q-9). Число задаётся командой службы.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
