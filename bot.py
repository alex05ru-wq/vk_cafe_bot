"""
Чат-бот кофейни для сообщества ВКонтакте (официальный API, Long Poll).

Умеет: меню кнопками, корзина, ответы на частые вопросы, предзаказ
(с собой / в зале, время, комментарий). Заказ приходит администратору
с кнопками «Готовится» и «Готов» — клиент сразу получает уведомление.

Запуск: python bot.py   (перед этим заполните .env)
"""
import json
import os
import threading
import time
from datetime import datetime

import vk_api
from dotenv import load_dotenv
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

from config import CAFE, FAQ, HOW, MENU, WHEN, ADDRESS

load_dotenv()
TOKEN = os.getenv('VK_TOKEN')            # ключ доступа сообщества
GROUP_ID = os.getenv('GROUP_ID')         # ID сообщества (только цифры)
ADMIN_ID = os.getenv('ADMIN_ID')         # ID страницы администратора ВКонтакте
if not TOKEN or not GROUP_ID:
    raise SystemExit('Заполните VK_TOKEN и GROUP_ID в файле .env (см. .env.example)')

session = vk_api.VkApi(token=TOKEN)
vk = session.get_api()

PRICES = {name: price for items in MENU.values() for name, price in items}
lock = threading.Lock()
carts = {}        # user_id -> {название: количество}
steps = {}        # user_id -> {'step': ..., 'how': ..., 'when': ...}
orders = {}       # номер заказа -> user_id клиента
counter = {'n': 100}


def rub(n):
    return f'{n:,} ₽'.replace(',', ' ')


def send(peer_id, text, keyboard=None):
    params = {'peer_id': peer_id, 'message': text, 'random_id': get_random_id()}
    if keyboard is not None:
        params['keyboard'] = keyboard.get_keyboard()
    vk.messages.send(**params)


# ---------- Клавиатуры ----------

def kb_main():
    kb = VkKeyboard()
    kb.add_button('☕ Меню', VkKeyboardColor.PRIMARY, payload={'a': 'menu'})
    kb.add_button('🛒 Корзина', VkKeyboardColor.SECONDARY, payload={'a': 'cart'})
    kb.add_line()
    kb.add_button('❓ Вопросы', VkKeyboardColor.SECONDARY, payload={'a': 'faq'})
    kb.add_button('✅ Оформить', VkKeyboardColor.POSITIVE, payload={'a': 'checkout'})
    return kb


def kb_categories():
    kb = VkKeyboard()
    for i, cat in enumerate(MENU):
        if i and i % 2 == 0:
            kb.add_line()
        kb.add_button(cat, VkKeyboardColor.PRIMARY, payload={'a': 'cat', 'c': cat})
    kb.add_line()
    kb.add_button('🛒 Корзина', VkKeyboardColor.SECONDARY, payload={'a': 'cart'})
    kb.add_button('⬅ Назад', VkKeyboardColor.SECONDARY, payload={'a': 'home'})
    return kb


def kb_items(cat):
    kb = VkKeyboard()
    for name, price in MENU[cat]:
        kb.add_button(f'{name} — {price} ₽'[:40], VkKeyboardColor.SECONDARY,
                      payload={'a': 'add', 'i': name})
        kb.add_line()
    kb.add_button('⬅ Категории', VkKeyboardColor.SECONDARY, payload={'a': 'menu'})
    kb.add_button('🛒 Корзина', VkKeyboardColor.PRIMARY, payload={'a': 'cart'})
    return kb


def kb_cart():
    kb = VkKeyboard()
    kb.add_button('✅ Оформить', VkKeyboardColor.POSITIVE, payload={'a': 'checkout'})
    kb.add_line()
    kb.add_button('☕ Добавить ещё', VkKeyboardColor.PRIMARY, payload={'a': 'menu'})
    kb.add_button('🗑 Очистить', VkKeyboardColor.NEGATIVE, payload={'a': 'clear'})
    return kb


def kb_choice(values, action):
    kb = VkKeyboard()
    for i, v in enumerate(values):
        if i and i % 2 == 0:
            kb.add_line()
        kb.add_button(v, VkKeyboardColor.PRIMARY, payload={'a': action, 'v': v})
    kb.add_line()
    kb.add_button('❌ Отмена', VkKeyboardColor.NEGATIVE, payload={'a': 'home'})
    return kb


def kb_comment():
    kb = VkKeyboard()
    kb.add_button('Без комментария', VkKeyboardColor.SECONDARY, payload={'a': 'comment', 'v': ''})
    kb.add_line()
    kb.add_button('❌ Отмена', VkKeyboardColor.NEGATIVE, payload={'a': 'home'})
    return kb


def kb_confirm():
    kb = VkKeyboard()
    kb.add_button('✅ Подтвердить заказ', VkKeyboardColor.POSITIVE, payload={'a': 'confirm'})
    kb.add_line()
    kb.add_button('❌ Отмена', VkKeyboardColor.NEGATIVE, payload={'a': 'home'})
    return kb


def kb_faq():
    kb = VkKeyboard()
    for q in FAQ:
        kb.add_button(q[:40], VkKeyboardColor.SECONDARY, payload={'a': 'q', 'q': q})
        kb.add_line()
    kb.add_button('⬅ Назад', VkKeyboardColor.SECONDARY, payload={'a': 'home'})
    return kb


def kb_status(n):
    kb = VkKeyboard(inline=True)
    kb.add_callback_button('👨‍🍳 Готовится', VkKeyboardColor.PRIMARY, payload={'st': 'cook', 'n': n})
    kb.add_callback_button('✅ Готов', VkKeyboardColor.POSITIVE, payload={'st': 'ready', 'n': n})
    return kb


# ---------- Корзина ----------

def cart_text(user_id):
    with lock:
        cart = dict(carts.get(user_id, {}))
    if not cart:
        return None, 0
    lines, total = [], 0
    for name, q in cart.items():
        total += PRICES[name] * q
        lines.append(f'• {name} × {q} — {rub(PRICES[name] * q)}')
    return '\n'.join(lines), total


# ---------- Обработка нажатий и сообщений ----------

def handle(user_id, text, payload):
    a = payload.get('a')
    with lock:
        state = steps.setdefault(user_id, {'step': None})

    if a == 'menu':
        send(user_id, 'Выберите категорию 👇', kb_categories())
    elif a == 'cat' and payload.get('c') in MENU:
        send(user_id, f'{payload["c"]}: нажмите на позицию, чтобы добавить в корзину', kb_items(payload['c']))
    elif a == 'add' and payload.get('i') in PRICES:
        name = payload['i']
        with lock:
            cart = carts.setdefault(user_id, {})
            cart[name] = cart.get(name, 0) + 1
            count = sum(cart.values())
        _, total = cart_text(user_id)
        cat = next(c for c, items in MENU.items() if name in dict(items))
        send(user_id, f'Добавил: {name}. В корзине {count} шт · {rub(total)}', kb_items(cat))
    elif a == 'cart':
        body, total = cart_text(user_id)
        if not body:
            send(user_id, 'Корзина пока пуста ☕', kb_categories())
        else:
            send(user_id, f'🛒 Ваш заказ:\n{body}\n\nИтого: {rub(total)}', kb_cart())
    elif a == 'clear':
        with lock:
            carts.pop(user_id, None)
        send(user_id, 'Корзина очищена.', kb_main())
    elif a == 'faq':
        send(user_id, 'Что вас интересует?', kb_faq())
    elif a == 'q' and payload.get('q') in FAQ:
        send(user_id, FAQ[payload['q']], kb_faq())
    elif a == 'checkout':
        if not cart_text(user_id)[0]:
            send(user_id, 'Сначала добавьте что-нибудь из меню ☕', kb_categories())
            return
        with lock:
            state.update(step='how')
        send(user_id, 'Как получить заказ?', kb_choice(HOW, 'how'))
    elif a == 'how' and payload.get('v') in HOW:
        with lock:
            state.update(step='when', how=payload['v'])
        send(user_id, 'Когда приготовить?', kb_choice(WHEN, 'when'))
    elif a == 'when' and payload.get('v') in WHEN:
        with lock:
            state.update(step='comment', when=payload['v'])
        send(user_id, 'Есть пожелания? Напишите сообщением (например: «без сахара») '
                      'или нажмите «Без комментария».', kb_comment())
    elif a == 'comment' or (state.get('step') == 'comment' and not a and text):
        comment = payload.get('v', '') if a else text.strip()[:300]
        with lock:
            state.update(step='confirm', comment=comment)
        body, total = cart_text(user_id)
        send(user_id, f'Проверьте заказ:\n{body}\n\nИтого: {rub(total)}\n'
                      f'{state["how"]} · {state["when"]}'
                      + (f'\n💬 {comment}' if comment else ''), kb_confirm())
    elif a == 'confirm' and state.get('step') == 'confirm':
        confirm_order(user_id, state)
    elif a == 'home' or text.lower() in ('начать', 'start', 'привет', 'меню'):
        with lock:
            state.update(step=None)
        send(user_id, f'Здравствуйте! Это {CAFE} ☕\n'
                      'Можно посмотреть меню, задать вопрос или оформить предзаказ — '
                      'приготовим к нужному времени.', kb_main())
    else:
        send(user_id, 'Пожалуйста, воспользуйтесь кнопками ниже 👇', kb_main())


def confirm_order(user_id, state):
    body, total = cart_text(user_id)
    if not body:
        send(user_id, 'Корзина пуста — начнём заново.', kb_main())
        return
    with lock:
        counter['n'] += 1
        n = counter['n']
        orders[n] = user_id
        carts.pop(user_id, None)
        state.update(step=None)
    details = f'{state["how"]} · {state["when"]}' + (f'\n💬 {state["comment"]}' if state.get('comment') else '')

    send(user_id, f'✅ Заказ №{n} принят!\n\n{body}\n\nИтого: {rub(total)}\n{details}\n📍 {ADDRESS}\n\n'
                  'Сообщим, когда заказ будет готов.', kb_main())
    if ADMIN_ID:
        try:
            name = vk.users.get(user_ids=user_id)[0]
            who = f'{name["first_name"]} {name["last_name"]} — vk.com/id{user_id}'
        except Exception:
            who = f'vk.com/id{user_id}'
        try:
            send(ADMIN_ID, f'🛎 Новый заказ №{n} — {datetime.now():%H:%M}\n\n{body}\n\n'
                           f'Итого: {rub(total)}\n{details}\n\nКлиент: {who}', kb_status(n))
        except vk_api.exceptions.ApiError as e:
            print('Не удалось отправить заказ администратору '
                  '(он должен сначала написать сообществу):', e)


def handle_status(event):
    obj = event.obj
    payload = obj.payload or {}
    n, st = payload.get('n'), payload.get('st')
    with lock:
        client = orders.get(n)
    if not client or st not in ('cook', 'ready'):
        note = 'Заказ не найден (бот перезапускался?)'
    else:
        text = (f'Ваш заказ №{n} готовится 👨‍🍳' if st == 'cook'
                else f'Ваш заказ №{n} готов! Ждём вас: {ADDRESS} ☕')
        send(client, text)
        note = 'Клиент получил уведомление'
    vk.messages.sendMessageEventAnswer(
        event_id=obj.event_id, user_id=obj.user_id, peer_id=obj.peer_id,
        event_data=json.dumps({'type': 'show_snackbar', 'text': note}, ensure_ascii=False))


def run():
    longpoll = VkBotLongPoll(session, int(GROUP_ID))
    print(f'Бот {CAFE} запущен. Остановить: Ctrl+C')
    for event in longpoll.listen():
        try:
            if event.type == VkBotEventType.MESSAGE_NEW:
                msg = event.obj.message
                if msg['peer_id'] != msg['from_id']:      # только личные сообщения
                    continue
                payload = json.loads(msg.get('payload') or '{}')
                handle(msg['from_id'], msg.get('text', ''), payload)
            elif event.type == VkBotEventType.MESSAGE_EVENT:
                handle_status(event)
        except Exception as e:
            print('Ошибка при обработке события:', e)


if __name__ == '__main__':
    while True:
        try:
            run()
        except KeyboardInterrupt:
            break
        except Exception as e:                          # обрыв связи — переподключаемся
            print('Связь прервалась, переподключение через 5 сек:', e)
            time.sleep(5)
