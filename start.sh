#!/bin/bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Нужен Python 3.10 или новее: https://www.python.org/downloads/"
  exit 1
fi

# Local virtual environment: no global pip installation and no copied macOS venv.
if [ ! -f ".venv/bin/python" ]; then
  echo "Создаём отдельное окружение Python..."
  python3 -m venv .venv
fi

".venv/bin/python" -m pip install --upgrade -r requirements.txt || {
  echo "Не удалось установить зависимости. Проверьте подключение к интернету."
  exit 1
}

echo ""
echo "SaveMedia готов: http://127.0.0.1:8000"
echo "Оставьте Терминал открытым. Для остановки нажмите Ctrl+C."
echo ""
".venv/bin/python" app.py
