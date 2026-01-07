# xoster

UserBot для автопостинга и зеркалирования Telegram каналов.

Фукнционал:

- можно добавлять сетку каналов
- текст проходит через API DeepSeek
- исключение дублирования новостей и рекламы
- реагирует на редактирование сообщения
- реализована работа с фотографиями, видео и альбомами

### Быстрый старт
1. Клонируем проект и создаём .env

- ```git clone https://github.com/MrSmIlEyar/xoster.git```
- ```cp .env.example .env```

2. Получаем необходимые креды для .env

- API_ID, API_HASH: [TelegramApps](https://my.telegram.org/apps) 
- DEEPSEEK_API_KEY: [DeepSeek API]( https://platform.deepseek.com)

- SOURCE_CHANNELS: придерживаясь формату шаблона вписать алиасы сетки каналов для зеркалирования (подписаться на них)
- TARGET_CHANNEL: вписать алиас своего канала

3. Запуск скрипта
- ```python -m venv .venv```
- ```pip install -r requirements.txt```
- ```python main.py```

Возможны неточности в обработке рекламных постов!
