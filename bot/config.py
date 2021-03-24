from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


EMPTY_DICT = {'source': None, 'target': None}
CONTINUE = 'Продолжить'
CHANGE_VIDEO = 'Поменять видео'

buttons_source = [KeyboardButton(CONTINUE),
                  KeyboardButton(CHANGE_VIDEO)]
markup_source = ReplyKeyboardMarkup(resize_keyboard=True,
                                    one_time_keyboard=True).add(*buttons_source)
