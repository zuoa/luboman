import requests
import telebot
from luboman.core.decorators import PluginTool
from luboman.core.notify import BaseNotifier


@PluginTool.notify('tg')
class TelegramNotifier(BaseNotifier):
    def __init__(self, token):
        super().__init__('tg', token)

    def do_notify(self, title, content):
        token, chat_id = self.token.split('______')

        bot = telebot.TeleBot(token)
        return bot.send_message(chat_id, f'{title}\n\n{content}')
