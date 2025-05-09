import logging
import sqlite3
import os
import json 
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
)

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "lt") 
RENDER_DISK_MOUNT_PATH = os.getenv("RENDER_DISK_MOUNT_PATH") 

translations = {}
ADMIN_IDS = [] 

# --- Database Path Setup ---
if RENDER_DISK_MOUNT_PATH:
    DB_FILE_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, "bot.db")
else:
    DB_FILE_PATH = "bot.db" 
DB_NAME = DB_FILE_PATH


def load_translations():
    global translations
    for lang_code in ["en", "lt"]:
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__)) 
            file_path = os.path.join(script_dir, "locales", f"{lang_code}.json")
            with open(file_path, "r", encoding="utf-8") as f:
                translations[lang_code] = json.load(f)
            logger.info(f"Successfully loaded translation file: {file_path}")
        except FileNotFoundError:
            logger.error(f"Translation file for {lang_code}.json not found at {file_path}")
        except json.JSONDecodeError as e: # Added 'e' to log the specific error
            logger.error(f"Error decoding JSON from {lang_code}.json at {file_path}: {e}")
    if not translations.get("en") or not translations.get("lt"):
        logger.error("Essential English or Lithuanian translation files are missing.")

async def get_user_language(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    if 'language_code' in context.user_data:
        return context.user_data['language_code']
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    result = None 
    try:
        cursor.execute("SELECT language_code FROM users WHERE telegram_id = ?", (user_id,))
        result = cursor.fetchone()
    except sqlite3.Error as e:
        logger.error(f"DB error in get_user_language for user {user_id}: {e}")
    finally:
        conn.close()
    
    if result and result[0]:
        context.user_data['language_code'] = result[0] 
        return result[0]
    
    context.user_data['language_code'] = DEFAULT_LANGUAGE 
    return DEFAULT_LANGUAGE

async def _(context: ContextTypes.DEFAULT_TYPE, key: str, user_id: int = None, **kwargs) -> str:
    actual_user_id_for_lang = user_id
    if actual_user_id_for_lang is None: 
        if context.effective_user: 
            actual_user_id_for_lang = context.effective_user.id
        elif 'user_id_for_translation' in context.chat_data: 
            actual_user_id_for_lang = context.chat_data['user_id_for_translation']

    lang_code = DEFAULT_LANGUAGE 
    if actual_user_id_for_lang: 
        lang_code = await get_user_language(context, actual_user_id_for_lang)
    
    text_to_return = translations.get(lang_code, {}).get(key)
    if text_to_return is None and lang_code != DEFAULT_LANGUAGE: 
        text_to_return = translations.get(DEFAULT_LANGUAGE, {}).get(key)
    if text_to_return is None and lang_code != "en" and DEFAULT_LANGUAGE != "en": 
         text_to_return = translations.get("en", {}).get(key)
    if text_to_return is None: 
        default_text = kwargs.pop("default", key) 
        logger.warning(f"Translation key '{key}' not found. Using default/key: '{default_text}'")
        text_to_return = default_text
    
    try:
        if isinstance(text_to_return, str) and (("{" in text_to_return and "}" in text_to_return) or not kwargs):
            return text_to_return.format(**kwargs)
        return str(text_to_return) 
    except KeyError as e: 
        logger.warning(f"Missing placeholder {e} for key '{key}' (lang '{lang_code}'). String: '{text_to_return}'. Kwargs: {kwargs}")
        return text_to_return 
    except Exception as e:
        logger.error(f"Error formatting string for key '{key}': {e}")
        return key

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def init_db():
    conn = sqlite3.connect(DB_NAME) # Uses the global DB_NAME
    cursor = conn.cursor()
    sql_create_users_table = f"""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
        is_admin INTEGER DEFAULT 0, language_code TEXT DEFAULT '{DEFAULT_LANGUAGE}'
    )"""
    cursor.execute(sql_create_users_table)
    cursor.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, price_per_kg REAL NOT NULL, is_available INTEGER DEFAULT 1)")
    cursor.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, user_name TEXT, order_date TEXT NOT NULL, total_price REAL NOT NULL, status TEXT DEFAULT 'pending', FOREIGN KEY (user_id) REFERENCES users (telegram_id))")
    cursor.execute("CREATE TABLE IF NOT EXISTS order_items (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity_kg REAL NOT NULL, price_at_order REAL NOT NULL, FOREIGN KEY (order_id) REFERENCES orders (id), FOREIGN KEY (product_id) REFERENCES products (id))")
    conn.commit()
    conn.close()

async def ensure_user_exists(user_id: int, first_name: str, username: str, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    is_admin_user = 1 if ADMIN_IDS and user_id in ADMIN_IDS else 0
    current_lang = DEFAULT_LANGUAGE 
    try:
        cursor.execute("SELECT language_code FROM users WHERE telegram_id = ?", (user_id,))
        user_record = cursor.fetchone()
        if user_record and user_record[0]: current_lang = user_record[0]
        context.user_data['language_code'] = current_lang 
        cursor.execute("INSERT INTO users (telegram_id, first_name, username, language_code, is_admin) VALUES (?, ?, ?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET first_name = excluded.first_name, username = excluded.username, is_admin = excluded.is_admin, language_code = COALESCE(users.language_code, excluded.language_code)", (user_id, first_name, username, current_lang, is_admin_user)) 
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"DB error in ensure_user_exists for user {user_id}: {e}")
        context.user_data['language_code'] = DEFAULT_LANGUAGE 
    finally: conn.close()
    return current_lang

async def set_user_language_db(user_id: int, lang_code: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET language_code = ? WHERE telegram_id = ?", (lang_code, user_id))
        conn.commit()
    except sqlite3.Error as e: logger.error(f"DB error in set_user_language_db for user {user_id}: {e}")
    finally: conn.close()

# --- Database Functions ---
def add_product_to_db(name, price):
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor()
    try: cursor.execute("INSERT INTO products (name, price_per_kg) VALUES (?, ?)", (name, price)); conn.commit(); return True
    except sqlite3.IntegrityError: logger.warning(f"Duplicate product: {name}"); return False
    except sqlite3.Error as e: logger.error(f"DB error adding product {name}: {e}"); return False
    finally: conn.close()
def get_products_from_db(available_only=True):
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); products = []
    try:
        query = "SELECT id, name, price_per_kg, is_available FROM products"
        if available_only: query += " WHERE is_available = 1"
        query += " ORDER BY name"; cursor.execute(query); products = cursor.fetchall()
    except sqlite3.Error as e: logger.error(f"DB error getting products: {e}")
    finally: conn.close()
    return products
def get_product_by_id(pid):
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); prod=None
    try: cursor.execute("SELECT id,name,price_per_kg,is_available FROM products WHERE id=?",(pid,)); prod=cursor.fetchone()
    except sqlite3.Error as e: logger.error(f"DB error get_product_by_id {pid}: {e}")
    finally: conn.close(); return prod
def update_product_in_db(pid,name=None,price=None,is_available=None):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); success=False; fields,params=[],[]
    if name is not None: fields.append("name=?"); params.append(name)
    if price is not None: fields.append("price_per_kg=?"); params.append(price)
    if is_available is not None: fields.append("is_available=?"); params.append(is_available)
    if not fields: conn.close(); return False
    params.append(pid); q=f"UPDATE products SET {','.join(fields)} WHERE id=?"
    try: cursor.execute(q,tuple(params)); conn.commit(); success=True
    except sqlite3.Error as e: logger.error(f"DB error update_product_in_db {pid}: {e}")
    finally: conn.close(); return success
def delete_product_from_db(pid):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); success=False
    try: cursor.execute("DELETE FROM products WHERE id=?",(pid,)); conn.commit(); success=True
    except sqlite3.Error as e: logger.error(f"DB error delete_product_from_db {pid}: {e}")
    finally: conn.close(); return success
def save_order_to_db(uid,uname,cart,total):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); oid=None; date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute("INSERT INTO orders (user_id,user_name,order_date,total_price) VALUES (?,?,?,?)",(uid,uname,date,total)); oid=cursor.lastrowid
        for item in cart: cursor.execute("INSERT INTO order_items (order_id,product_id,quantity_kg,price_at_order) VALUES (?,?,?,?)",(oid,item['id'],item['quantity'],item['price']))
        conn.commit()
    except sqlite3.Error as e: logger.error(f"DB error save_order_to_db {uid}: {e}"); oid=None
    finally: conn.close(); return oid
def get_user_orders_from_db(uid):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); orders=[]
    try: cursor.execute("SELECT o.id,o.order_date,o.total_price,o.status,group_concat(p.name||' ('||oi.quantity_kg||'kg)',', ') FROM orders o JOIN order_items oi ON o.id=oi.order_id JOIN products p ON oi.product_id=p.id WHERE o.user_id=? GROUP BY o.id ORDER BY o.order_date DESC",(uid,)); orders=cursor.fetchall()
    except sqlite3.Error as e: logger.error(f"DB error get_user_orders_from_db {uid}: {e}")
    finally: conn.close(); return orders
def get_all_orders_from_db():
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); orders=[]
    try: cursor.execute("SELECT o.id,o.user_id,o.user_name,o.order_date,o.total_price,o.status,GROUP_CONCAT(p.name||' ('||oi.quantity_kg||'kg @ '||oi.price_at_order||' EUR)',CHAR(10)) as items_details FROM orders o JOIN order_items oi ON o.id=oi.order_id JOIN products p ON oi.product_id=p.id GROUP BY o.id ORDER BY o.order_date DESC"); orders=cursor.fetchall()
    except sqlite3.Error as e: logger.error(f"DB error get_all_orders_from_db: {e}")
    finally: conn.close(); return orders
def get_shopping_list_from_db():
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); slist=[]
    try: cursor.execute("SELECT p.name,SUM(oi.quantity_kg) as total_quantity FROM order_items oi JOIN products p ON oi.product_id=p.id JOIN orders o ON oi.order_id=o.id WHERE o.status IN ('pending','confirmed') GROUP BY p.name ORDER BY p.name"); slist=cursor.fetchall()
    except sqlite3.Error as e: logger.error(f"DB error get_shopping_list_from_db: {e}")
    finally: conn.close(); return slist
# --- End Database Functions ---

# --- Conversation States ---
(SELECT_LANGUAGE_STATE, 
 ORDER_FLOW_BROWSING_PRODUCTS, ORDER_FLOW_SELECTING_QUANTITY, ORDER_FLOW_VIEWING_CART,
 ADMIN_MAIN_PANEL_STATE, 
 ADMIN_ADD_PROD_NAME, ADMIN_ADD_PROD_PRICE, 
 ADMIN_MANAGE_PROD_LIST, ADMIN_MANAGE_PROD_OPTIONS, ADMIN_MANAGE_PROD_EDIT_PRICE, ADMIN_MANAGE_PROD_DELETE_CONFIRM
) = range(11) 

# --- Helper: Display Main Menu ---
async def display_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    user = update.effective_user
    user_id = user.id
    if 'language_code' not in context.user_data: 
        context.user_data['language_code'] = await get_user_language(context, user_id)

    keyboard = [
        [InlineKeyboardButton(await _(context, "browse_products_button", user_id=user_id), callback_data="order_flow_browse_entry")],
        [InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="order_flow_view_cart_direct_entry")], 
        [InlineKeyboardButton(await _(context, "my_orders_button", user_id=user_id), callback_data="my_orders_direct_cb")], 
        [InlineKeyboardButton(await _(context, "set_language_button", user_id=user_id), callback_data="select_language_entry")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = await _(context, "welcome_message", user_id=user_id, user_mention=user.mention_html())
    
    target_message = update.callback_query.message if edit_message and update.callback_query else update.message
    try:
        if edit_message and target_message:
            await target_message.edit_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')
        elif update.message: 
            await update.message.reply_html(welcome_text, reply_markup=reply_markup)
        elif user_id: # Fallback if no direct message to reply/edit (e.g. after certain conversation ends)
             await context.bot.send_message(chat_id=user_id, text=welcome_text, reply_markup=reply_markup, parse_mode='HTML')
    except Exception as e:
        logger.warning(f"Display main menu error (edit={edit_message}): {e}. Sending new message.")
        if user_id: await context.bot.send_message(chat_id=user_id, text=welcome_text, reply_markup=reply_markup, parse_mode='HTML')
    # This function purely displays. State transitions are handled by ConversationHandlers or CommandHandlers.

# --- Start Command & General Back to Main Menu ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    user = update.effective_user
    await ensure_user_exists(user.id, user.first_name or "", user.username or "", context) 
    lang = context.user_data.get('language_code')
    # Clear only conversation-specific data, preserve language
    keys_to_clear = [k for k in context.user_data if k != 'language_code']
    for k in keys_to_clear: context.user_data.pop(k, None)
    
    if 'language_code' not in context.user_data : # Should be set by ensure_user_exists
         context.user_data['language_code'] = await get_user_language(context, user.id)
    await display_main_menu(update, context)

async def back_to_main_menu_cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query: await update.callback_query.answer()
    
    lang = context.user_data.get('language_code')
    keys_to_clear = [k for k in context.user_data if k != 'language_code']
    for k in keys_to_clear: context.user_data.pop(k, None)
    if lang: context.user_data['language_code'] = lang # Restore if it was popped
    
    await display_main_menu(update, context, edit_message=bool(update.callback_query))
    return ConversationHandler.END 

# --- Language Selection Flow ---
async def select_language_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    keyboard = [
        [InlineKeyboardButton("English 🇬🇧", callback_data="lang_select_en")],
        [InlineKeyboardButton("Lietuvių 🇱🇹", callback_data="lang_select_lt")],
        [InlineKeyboardButton(await _(context, "back_button", user_id=user_id, default="⬅️ Back"), callback_data="main_menu_direct_cb_ender")]
    ]
    await query.edit_message_text(await _(context, "choose_language", user_id=user_id), reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_LANGUAGE_STATE

async def language_selected_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); lang_code = query.data.split('_')[-1]; user_id = query.from_user.id
    context.user_data['language_code'] = lang_code; await set_user_language_db(user_id, lang_code)
    lang_name = "English" if lang_code == "en" else "Lietuvių"
    await query.edit_message_text(await _(context, "language_set_to", user_id=user_id, language_name=lang_name))
    await display_main_menu(update, context, edit_message=True)
    return ConversationHandler.END

# --- User Order Flow ---
async def order_flow_browse_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info(f"User {update.effective_user.id} entered order_flow_browse_entry via CB: {update.callback_query.data}")
    query = update.callback_query; await query.answer()
    return await order_flow_list_products(update, context, query.from_user.id, edit_message=True)

async def order_flow_list_products(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, edit_message: bool = True) -> int:
    query = update.callback_query # This function is mostly called from callbacks
    products = get_products_from_db(available_only=True)
    keyboard, text_to_send = [], ""
    if not products:
        text_to_send = await _(context, "no_products_available", user_id=user_id)
        keyboard.append([InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="main_menu_direct_cb_ender")])
    else:
        text_to_send = await _(context, "products_title", user_id=user_id)
        for pid, name, price, _avail in products: 
            keyboard.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR/kg", callback_data=f"order_flow_select_prod_{pid}")])
        keyboard.append([InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="order_flow_view_cart_state_cb")])
        keyboard.append([InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="main_menu_direct_cb_ender")])
    
    target_message = query.message if query else update.message 
    try:
        if edit_message and query and target_message: 
            await target_message.edit_text(text=text_to_send, reply_markup=InlineKeyboardMarkup(keyboard))
        elif update.message and target_message : # If called after a text message (e.g. quantity typed)
            await target_message.reply_text(text=text_to_send, reply_markup=InlineKeyboardMarkup(keyboard))
        elif query and not edit_message and target_message: # e.g. "add more" after quantity, should be new msg
             await target_message.reply_text(text=text_to_send, reply_markup=InlineKeyboardMarkup(keyboard))
        else: 
            await context.bot.send_message(chat_id=user_id, text=text_to_send, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error in order_flow_list_products (edit={edit_message}): {e}")
        await context.bot.send_message(chat_id=user_id, text=text_to_send, reply_markup=InlineKeyboardMarkup(keyboard)) # Fallback
    return ORDER_FLOW_BROWSING_PRODUCTS

async def order_flow_product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    try: product_id = int(query.data.split('_')[-1])
    except: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); return ORDER_FLOW_BROWSING_PRODUCTS
    product = get_product_by_id(product_id)
    if not product: await query.edit_message_text(await _(context,"product_not_found",user_id=user_id, default="Not found.")); return ORDER_FLOW_BROWSING_PRODUCTS
    context.user_data.update({'current_product_id':product_id, 'current_product_name':product[1], 'current_product_price':product[2]})
    await query.edit_message_text(await _(context,"product_selected_prompt",user_id=user_id,product_name=product[1]))
    return ORDER_FLOW_SELECTING_QUANTITY

async def order_flow_quantity_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; quantity_str = update.message.text
    try: quantity = float(quantity_str); assert quantity > 0
    except: await update.message.reply_text(await _(context,"invalid_quantity_prompt",user_id=user_id)); return ORDER_FLOW_SELECTING_QUANTITY
    
    pid = context.user_data.get('current_product_id')
    pname = context.user_data.get('current_product_name')
    pprice = context.user_data.get('current_product_price')

    if not all([pid is not None, pname is not None, pprice is not None]): 
        await update.message.reply_text(await _(context,"generic_error_message",user_id=user_id,default="Error. Please try adding product again.")); 
        return await order_flow_list_products(update, context, user_id, edit_message=False) # Show product list as new message

    cart = context.user_data.setdefault('cart', [])
    found = any(item['id'] == pid and (item.update({'quantity': item['quantity'] + quantity}) or True) for item in cart)
    if not found: cart.append({'id':pid,'name':pname,'price':pprice,'quantity':quantity})
    
    await update.message.reply_text(await _(context,"item_added_to_cart",user_id=user_id,quantity=quantity,product_name=pname))
    keyboard = [ 
        [InlineKeyboardButton(await _(context,"add_more_products_button",user_id=user_id), callback_data="order_flow_browse_return_cb")], 
        [InlineKeyboardButton(await _(context,"view_cart_button",user_id=user_id), callback_data="order_flow_view_cart_state_cb")], 
        [InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=user_id), callback_data="main_menu_direct_cb_ender")]
    ]
    await update.message.reply_text(await _(context,"what_next_prompt",user_id=user_id),reply_markup=InlineKeyboardMarkup(keyboard))
    # This state needs to handle the above buttons.
    return ORDER_FLOW_BROWSING_PRODUCTS # All buttons lead to actions handled in this state or end the conv

async def order_flow_view_cart_state_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    return await order_flow_display_cart(update, context, user_id, edit_message=True)

async def order_flow_view_cart_direct_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    context.user_data.setdefault('cart', []) 
    await order_flow_display_cart(update, context, user_id, edit_message=True)
    return ORDER_FLOW_VIEWING_CART 

async def order_flow_display_cart(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, edit_message: bool) -> int: # Return state
    cart = context.user_data.get('cart', [])
    target_message = update.callback_query.message if edit_message and update.callback_query else update.message
    
    text, keyboard_buttons = "", []
    if not cart:
        text = await _(context,"cart_empty",user_id=user_id)
        keyboard_buttons.append([InlineKeyboardButton(await _(context,"browse_products_button",user_id=user_id),callback_data="order_flow_browse_return_cb")])
    else:
        text = await _(context,"your_cart_title",user_id=user_id)+"\n"; total_price=0
        for i,item in enumerate(cart):
            item_total=item['price']*item['quantity']; total_price+=item_total
            text+=f"{i+1}. {item['name']} - {item['quantity']} kg x {item['price']:.2f} EUR = {item_total:.2f} EUR\n"
            keyboard_buttons.append([InlineKeyboardButton(await _(context,"remove_item_button",user_id=user_id,item_index=i+1),callback_data=f"order_flow_remove_item_{i}")])
        text+="\n"+await _(context,"cart_total",user_id=user_id,total_price=total_price)
        keyboard_buttons.append([InlineKeyboardButton(await _(context,"checkout_button",user_id=user_id),callback_data="order_flow_checkout_cb")])
        keyboard_buttons.append([InlineKeyboardButton(await _(context,"add_more_products_button",user_id=user_id),callback_data="order_flow_browse_return_cb")])

    keyboard_buttons.append([InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=user_id),callback_data="main_menu_direct_cb_ender")])
    
    try:
        if edit_message and target_message: await target_message.edit_text(text=text,reply_markup=InlineKeyboardMarkup(keyboard_buttons))
        elif update.message and target_message : await target_message.reply_text(text=text,reply_markup=InlineKeyboardMarkup(keyboard_buttons))
        else: await context.bot.send_message(chat_id=user_id, text=text,reply_markup=InlineKeyboardMarkup(keyboard_buttons))
    except Exception as e:
        logger.error(f"Error display_cart: {e}")
        await context.bot.send_message(chat_id=user_id, text=text,reply_markup=InlineKeyboardMarkup(keyboard_buttons))

    return ORDER_FLOW_VIEWING_CART

async def order_flow_remove_item_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query=update.callback_query; await query.answer(); user_id=query.from_user.id
    try: idx=int(query.data.split('_')[-1])
    except: await query.message.reply_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); return ORDER_FLOW_VIEWING_CART
    cart=context.user_data.get('cart',[])
    if 0<=idx<len(cart): removed=cart.pop(idx); await query.message.reply_text(await _(context,"item_removed_from_cart",user_id=user_id,item_name=removed['name']))
    else: await query.message.reply_text(await _(context,"invalid_item_to_remove",user_id=user_id))
    return await order_flow_display_cart(update,context,user_id,edit_message=True)

async def order_flow_checkout_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    cart = context.user_data.get('cart', [])
    if not cart: await query.edit_message_text(await _(context, "cart_empty", user_id=user_id)); return ORDER_FLOW_VIEWING_CART 

    user = query.effective_user
    user_full_name_str = user.full_name or "N/A"; user_username_str = user.username or "N/A"
    total_price = sum(item['price'] * item['quantity'] for item in cart)
    order_id = save_order_to_db(user_id, user_full_name_str, cart, total_price)
    admin_lang_user_id = ADMIN_IDS[0] if ADMIN_IDS else None

    if order_id:
        await query.edit_message_text(await _(context, "order_placed_success", user_id=user_id, order_id=order_id, total_price=total_price))
        
        admin_title = await _(context,"admin_new_order_notification_title",user_id=admin_lang_user_id,order_id=order_id,default=f"🔔 New Order #{order_id}")
        admin_msg = f"{admin_title}\n"
        admin_msg += await _(context,"admin_order_from",user_id=admin_lang_user_id,name=user_full_name_str,username=user_username_str,customer_id=user_id,default=f"From: {user_full_name_str}...") + "\n\n"
        admin_msg += await _(context,"admin_order_items_header",user_id=admin_lang_user_id,default="Items:")+"\n------------------------------------\n"
        item_lines = []
        for i, citem in enumerate(cart):
            sub = citem['price']*citem['quantity']
            item_lines.append(await _(context,"admin_order_item_line_format",user_id=admin_lang_user_id,index=i+1,item_name=citem['name'],quantity=citem['quantity'],price_per_kg=citem['price'],item_subtotal=sub,default=f"{i+1}. {citem['name']}..."))
        admin_msg += "\n".join(item_lines) + "\n------------------------------------\n"
        admin_msg += await _(context,"admin_order_grand_total",user_id=admin_lang_user_id,total_price=total_price,default=f"Total: {total_price:.2f} EUR")

        if ADMIN_IDS: 
            for admin_id_val in ADMIN_IDS: 
                try:
                    if len(admin_msg)>4096: 
                        for i_part in range(0, len(admin_msg), 4096): await context.bot.send_message(chat_id=admin_id_val, text=admin_msg[i_part:i_part+4096])
                    else: await context.bot.send_message(chat_id=admin_id_val, text=admin_msg)
                except Exception as e: logger.error(f"Notify admin {admin_id_val} error: {e}")
        
        lang=context.user_data.get('language_code'); 
        keys_to_clear = ['cart', 'current_product_id', 'current_product_name', 'current_product_price']
        for key_to_pop in keys_to_clear: context.user_data.pop(key_to_pop, None)
        if lang: context.user_data['language_code']=lang 
        
        # Create a new message for the main menu
        # Since query.message was edited for order confirmation, we need a new message context for display_main_menu
        # A simple way is to just send it, not try to make display_main_menu edit.
        await display_main_menu(update, context, edit_message=False) # Pass original update, display_main_menu will handle sending new.
    else: 
        await query.edit_message_text(await _(context,"order_placed_error",user_id=user_id))
        kb = [[InlineKeyboardButton(await _(context,"view_cart_button",user_id=user_id),callback_data="order_flow_view_cart_state_cb")],
              [InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=user_id),callback_data="main_menu_direct_cb_ender")]]
        what_next_text = await _(context, "what_next_prompt", user_id=user_id, default="What would you like to do?")
        try: await query.message.reply_text(what_next_text, reply_markup=InlineKeyboardMarkup(kb))
        except: await context.bot.send_message(chat_id=user_id,text=what_next_text,reply_markup=InlineKeyboardMarkup(kb))
        return ORDER_FLOW_VIEWING_CART 
    return ConversationHandler.END

async def my_orders_direct_cb(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query=update.callback_query; await query.answer(); user_id=query.from_user.id
    orders=get_user_orders_from_db(user_id)
    text = await _(context,"my_orders_title",user_id=user_id, default="Your Orders:")+"\n\n" if orders else await _(context,"no_orders_yet",user_id=user_id)
    if orders:
        for oid,date,total,status,items in orders: text+=await _(context,"order_details_format",user_id=user_id,order_id=oid,date=date,status=status.capitalize(),total=total,items=items, default="Order #{order_id}...")
    kb=[[InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=user_id),callback_data="main_menu_direct_cb_ender")]]
    await query.edit_message_text(text=text,reply_markup=InlineKeyboardMarkup(kb))

# --- Admin Panel and Flows ---
async def display_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False) -> int:
    user = update.effective_user; user_id = user.id
    if not (ADMIN_IDS and user_id in ADMIN_IDS): 
        unauth_text = await _(context,"admin_unauthorized",user_id=user_id)
        target_msg = update.callback_query.message if edit_message and update.callback_query else update.message
        if edit_message and target_msg: await target_msg.edit_text(unauth_text)
        elif update.message: await update.message.reply_text(unauth_text)
        else: await context.bot.send_message(chat_id=user_id, text=unauth_text)
        return ConversationHandler.END 
    
    context.chat_data['user_id_for_translation'] = user_id 
    kb = [
        [InlineKeyboardButton(await _(context,"admin_add_product_button",user_id=user_id),callback_data="admin_add_prod_entry_cb")],
        [InlineKeyboardButton(await _(context,"admin_manage_products_button",user_id=user_id),callback_data="admin_manage_prod_list_entry_cb")],
        [InlineKeyboardButton(await _(context,"admin_view_orders_button",user_id=user_id),callback_data="admin_view_orders_direct_cb")],
        [InlineKeyboardButton(await _(context,"admin_shopping_list_button",user_id=user_id),callback_data="admin_shop_list_direct_cb")],
        [InlineKeyboardButton(await _(context,"admin_exit_button",user_id=user_id),callback_data="main_menu_direct_cb_ender")] 
    ]
    title = await _(context,"admin_panel_title",user_id=user_id)
    target_msg = update.callback_query.message if edit_message and update.callback_query else update.message
    try:
        if edit_message and target_msg: await target_msg.edit_text(title,reply_markup=InlineKeyboardMarkup(kb))
        elif update.message : await update.message.reply_text(title,reply_markup=InlineKeyboardMarkup(kb))
        else: await context.bot.send_message(chat_id=user_id, text=title,reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: logger.warning(f"Display admin panel error: {e}"); await context.bot.send_message(chat_id=user_id, text=title,reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MAIN_PANEL_STATE

async def admin_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await display_admin_panel(update,context)
async def admin_panel_return_direct_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    if update.callback_query: await update.callback_query.answer()
    context.user_data.pop('editing_pid', None) 
    return await display_admin_panel(update,context,edit_message=True)

# Admin Add Product
async def admin_add_prod_entry_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query; await q.answer(); uid=q.from_user.id
    await q.edit_message_text(await _(context,"admin_enter_product_name",user_id=uid))
    return ADMIN_ADD_PROD_NAME
async def admin_add_prod_name_state(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    uid=update.effective_user.id; pname=update.message.text; context.user_data['new_pname']=pname
    await update.message.reply_text(await _(context,"admin_enter_product_price",user_id=uid,product_name=pname))
    return ADMIN_ADD_PROD_PRICE
async def admin_add_prod_price_state(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    user_id=update.effective_user.id; name=context.user_data.get('new_pname')
    try: price_str = update.message.text; price=float(price_str); assert price>0
    except: await update.message.reply_text(await _(context,"admin_invalid_price",user_id=user_id)); return ADMIN_ADD_PROD_PRICE
    if not name: 
        await update.message.reply_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); 
        return await display_admin_panel(update, context, edit_message=False) 

    format_kwargs = {'user_id': user_id, 'product_name': name}
    msg_key = "admin_product_added" if add_product_to_db(name,price) else "admin_product_add_failed"
    if msg_key == "admin_product_added": format_kwargs['price'] = price
    await update.message.reply_text(await _(context,msg_key,**format_kwargs))
    if 'new_pname' in context.user_data: del context.user_data['new_pname']
    await display_admin_panel(update, context, edit_message=False) 
    return ConversationHandler.END

# Admin Manage Products
async def admin_manage_prod_list_entry_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    context.user_data.pop('editing_pid', None) 
    products = get_products_from_db(available_only=False)
    keyboard, text = [], ""
    if not products:
        text = await _(context, "admin_no_products_to_manage", user_id=user_id)
        keyboard.append([InlineKeyboardButton(await _(context, "admin_back_to_admin_panel_button", user_id=user_id), callback_data="admin_panel_return_direct_cb")])
    else:
        text = await _(context, "admin_select_product_to_manage", user_id=user_id)
        for pid, name, price, avail in products:
            status_key = "admin_status_available" if avail else "admin_status_unavailable"
            status = await _(context, status_key, user_id=user_id, default="Available" if avail else "Unavailable")
            keyboard.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR ({status})", callback_data=f"admin_manage_select_prod_{pid}")])
        keyboard.append([InlineKeyboardButton(await _(context, "admin_back_to_admin_panel_button", user_id=user_id), callback_data="admin_panel_return_direct_cb")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_MANAGE_PROD_LIST

async def admin_manage_prod_selected_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    try: product_id = int(query.data.split('_')[-1])
    except: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); return ADMIN_MANAGE_PROD_LIST
    product = get_product_by_id(product_id)
    if not product: await query.edit_message_text(await _(context,"product_not_found",user_id=user_id, default="Not found.")); return ADMIN_MANAGE_PROD_LIST
    context.user_data['editing_pid'] = product_id
    pname, pprice, pavail = product[1], product[2], product[3]
    avail_btn_key = "admin_set_unavailable_button" if pavail else "admin_set_available_button"
    kb = [
        [InlineKeyboardButton(await _(context,"admin_change_price_button",user_id=user_id,price=pprice), callback_data="admin_manage_edit_price_entry_cb")],
        [InlineKeyboardButton(await _(context,avail_btn_key,user_id=user_id), callback_data=f"admin_manage_toggle_avail_cb_{1-pavail}")],
        [InlineKeyboardButton(await _(context,"admin_delete_product_button",user_id=user_id), callback_data="admin_manage_delete_confirm_cb")],
        [InlineKeyboardButton(await _(context,"admin_back_to_product_list_button",user_id=user_id), callback_data="admin_manage_prod_list_refresh_cb")]
    ]
    await query.edit_message_text(await _(context,"admin_managing_product",user_id=user_id,product_name=pname), reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MANAGE_PROD_OPTIONS

async def admin_manage_edit_price_entry_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); return ADMIN_MANAGE_PROD_LIST
    product = get_product_by_id(editing_pid)
    if not product: await query.edit_message_text(await _(context,"product_not_found",user_id=user_id, default="Not found.")); return ADMIN_MANAGE_PROD_LIST
    await query.edit_message_text(await _(context,"admin_enter_new_price",user_id=user_id,product_name=product[1],current_price=product[2]))
    return ADMIN_MANAGE_PROD_EDIT_PRICE

async def admin_manage_edit_price_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; new_price_str = update.message.text
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: 
        await update.message.reply_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); 
        return await admin_panel_return_direct_cb(update,context) 
    try: new_price = float(new_price_str); assert new_price > 0
    except: await update.message.reply_text(await _(context,"admin_invalid_price",user_id=user_id)); return ADMIN_MANAGE_PROD_EDIT_PRICE
    
    msg_key = "admin_price_updated" if update_product_in_db(editing_pid,price=new_price) else "admin_price_update_failed"
    await update.message.reply_text(await _(context,msg_key,user_id=user_id,product_id=editing_pid))
    
    # Go back to options for the same product by simulating a callback query
    # This needs to edit the message that showed "Enter new price..." or the user's price message.
    # Best to edit the message that was last sent by the bot if possible (the prompt for price).
    # However, the current `update` is the user's message.
    # We need to find the bot's last message that needs editing for the options.
    # This is complex. Simpler: Go back to product list.
    
    # Create a mock query object to call admin_manage_prod_list_entry_cb
    # to refresh the list after a message handler.
    # We need `update.message` to be the one to be edited by `admin_manage_prod_list_entry_cb`
    # if it were to edit. But it's a new list.
    class MockQuery: pass
    mock_query_obj = MockQuery()
    mock_query_obj.from_user = update.effective_user
    # The "message to edit" should ideally be the one that showed options or list.
    # Since we are after a user message, there isn't a bot message to readily edit for the list.
    # So, admin_manage_prod_list_entry_cb will send a new message.
    mock_query_obj.message = None # Indicate no message to edit, so it sends new.
    async def temp_answer(): pass
    mock_query_obj.answer = temp_answer
    
    # Construct an update-like object that admin_manage_prod_list_entry_cb expects.
    # It needs a callback_query attribute.
    mock_update_for_list = Update(update_id=0, callback_query=mock_query_obj) 
    mock_update_for_list.effective_user = update.effective_user # Ensure effective_user is set
    
    return await admin_manage_prod_list_entry_cb(mock_update_for_list, context)

async def admin_manage_toggle_avail_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); return ADMIN_MANAGE_PROD_LIST
    try: new_avail_state = int(query.data.split('_')[-1])
    except: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); return ADMIN_MANAGE_PROD_OPTIONS
    
    success = update_product_in_db(editing_pid,is_available=new_avail_state)
    status_text_key = "admin_status_available_text" if new_avail_state else "admin_status_unavailable_text"
    status_text = await _(context,status_text_key, user_id=user_id, default="available" if new_avail_state else "unavailable")
    msg_key = "admin_product_set_status" if success else "admin_status_update_failed"
    await query.edit_message_text(await _(context,msg_key,user_id=user_id,product_id=editing_pid,status_text=status_text))
    # Refresh options for the same product
    query.data = f"admin_manage_select_prod_{editing_pid}" # This will re-trigger selected_cb
    return await admin_manage_prod_selected_cb(update, context)

async def admin_manage_delete_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); return ADMIN_MANAGE_PROD_LIST
    product = get_product_by_id(editing_pid)
    if not product: await query.edit_message_text(await _(context,"product_not_found",user_id=user_id, default="Not found.")); return ADMIN_MANAGE_PROD_LIST
    kb = [
        [InlineKeyboardButton(await _(context,"admin_confirm_delete_yes_button",user_id=user_id,product_name=product[1]), callback_data="admin_manage_delete_do_cb")],
        [InlineKeyboardButton(await _(context,"admin_confirm_delete_no_button",user_id=user_id), callback_data=f"admin_manage_select_prod_{editing_pid}")]
    ]
    await query.edit_message_text(await _(context,"admin_confirm_delete_prompt",user_id=user_id,product_name=product[1]),reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MANAGE_PROD_DELETE_CONFIRM

async def admin_manage_delete_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id, default="Error.")); return ADMIN_MANAGE_PROD_LIST
    msg_key = "admin_product_deleted" if delete_product_from_db(editing_pid) else "admin_product_delete_failed"
    await query.edit_message_text(await _(context,msg_key,user_id=user_id,product_id=editing_pid))
    if 'editing_pid' in context.user_data: del context.user_data['editing_pid']
    return await admin_manage_prod_list_entry_cb(update, context)

# Direct Admin Actions
async def admin_view_orders_direct_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer(); user_id=query.from_user.id 
    orders=get_all_orders_from_db()
    text = await _(context,"admin_all_orders_title",user_id=user_id, default="All Orders:") if orders else await _(context,"admin_no_orders_found",user_id=user_id)
    if orders:
        for oid, cust_id_db, uname, date_val, total_val, status_val, items_val in orders: 
            text+=await _(context,"admin_order_details_format",user_id=user_id,order_id=oid,user_name=uname,customer_id=cust_id_db,date=date_val,total=total_val,status=status_val.capitalize(),items=items_val, default="Order...")
    if len(text)>4000: text=text[:4000]+"\n...(truncated)"
    kb=[[InlineKeyboardButton(await _(context,"admin_back_to_admin_panel_button",user_id=user_id),callback_data="admin_panel_return_direct_cb")]]
    await query.edit_message_text(text=text,reply_markup=InlineKeyboardMarkup(kb))

async def admin_shop_list_direct_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer(); user_id=query.from_user.id
    slist=get_shopping_list_from_db()
    text=await _(context,"admin_shopping_list_title",user_id=user_id, default="Shopping List:") if slist else await _(context,"admin_shopping_list_empty",user_id=user_id)
    if slist:
        for name,qty in slist: text+=await _(context,"admin_shopping_list_item_format",user_id=user_id,name=name,total_quantity=qty, default="- {name}: {total_quantity}kg\n")
    kb=[[InlineKeyboardButton(await _(context,"admin_back_to_admin_panel_button",user_id=user_id),callback_data="admin_panel_return_direct_cb")]]
    await query.edit_message_text(text=text,reply_markup=InlineKeyboardMarkup(kb))

# General Cancel Handler
async def general_cancel_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    edit_message, target_message = False, update.message
    if update.callback_query: await update.callback_query.answer(); target_message = update.callback_query.message; edit_message = True

    cancel_text = await _(context, "action_cancelled", user_id=user_id, default="Action cancelled.")
    try:
        if edit_message and target_message: await target_message.edit_text(cancel_text)
        elif target_message: await target_message.reply_text(cancel_text, reply_markup=ReplyKeyboardRemove())
        else: await context.bot.send_message(chat_id=user_id, text=cancel_text)
    except Exception as e: 
        logger.warning(f"Cancel handler error: {e}")
        # Ensure a message is sent if edit/reply fails but chat context exists
        if update.effective_chat and not (edit_message and target_message): # Avoid double send if edit failed
            await context.bot.send_message(chat_id=update.effective_chat.id, text=cancel_text)


    lang = context.user_data.get('language_code')
    keys_to_pop = ['cart','current_product_id','current_product_name','current_product_price','new_pname','editing_pid']
    for key in keys_to_pop: context.user_data.pop(key, None)
    if lang: context.user_data['language_code'] = lang
    
    if ADMIN_IDS and user_id in ADMIN_IDS:
        await display_admin_panel(update, context, edit_message=edit_message if target_message else False) 
    else:
        await display_main_menu(update, context, edit_message=edit_message if target_message else False)
    return ConversationHandler.END

def main() -> None:
    global ADMIN_IDS 
    if not TELEGRAM_TOKEN: logger.critical("TELEGRAM_TOKEN missing!"); return
    if not ADMIN_TELEGRAM_ID: logger.critical("ADMIN_TELEGRAM_ID missing!"); return
    try: ADMIN_IDS = [int(aid.strip()) for aid in ADMIN_TELEGRAM_ID.split(',')]
    except: logger.critical("Admin IDs invalid!"); return
    
    load_translations(); 
    if not translations.get("en") or not translations.get("lt"): logger.critical("Translations missing after load attempt!"); return
    init_db()
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    general_conv_fallbacks = [
        CallbackQueryHandler(back_to_main_menu_cb_handler, pattern="^main_menu_direct_cb_ender$"),
        CommandHandler("cancel", general_cancel_command_handler),
        CommandHandler("start", start_command) 
    ]
    admin_conv_fallbacks = [
        CallbackQueryHandler(admin_panel_return_direct_cb, pattern="^admin_panel_return_direct_cb$"), 
        CommandHandler("cancel", general_cancel_command_handler), 
        CommandHandler("admin", admin_command_entry) 
    ]

    lang_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_language_entry, pattern="^select_language_entry$")],
        states={SELECT_LANGUAGE_STATE: [CallbackQueryHandler(language_selected_state, pattern="^lang_select_(en|lt)$")]},
        fallbacks=general_conv_fallbacks
    )

    order_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(order_flow_browse_entry, pattern="^order_flow_browse_entry$"),
            CallbackQueryHandler(order_flow_view_cart_direct_entry, pattern="^order_flow_view_cart_direct_entry$")
        ],
        states={
            ORDER_FLOW_BROWSING_PRODUCTS: [
                CallbackQueryHandler(order_flow_product_selected, pattern="^order_flow_select_prod_\d+$"),
                CallbackQueryHandler(order_flow_view_cart_state_cb, pattern="^order_flow_view_cart_state_cb$"),
                CallbackQueryHandler(lambda u,c: order_flow_list_products(u,c,u.callback_query.from_user.id, edit_message=True), pattern="^order_flow_browse_return_cb$"),
            ],
            ORDER_FLOW_SELECTING_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_flow_quantity_typed)],
            ORDER_FLOW_VIEWING_CART: [
                CallbackQueryHandler(order_flow_remove_item_cb, pattern="^order_flow_remove_item_\d+$"),
                CallbackQueryHandler(order_flow_checkout_cb, pattern="^order_flow_checkout_cb$"),
                CallbackQueryHandler(lambda u,c: order_flow_list_products(u,c,u.callback_query.from_user.id, edit_message=True), pattern="^order_flow_browse_return_cb$"),
            ]
        },
        fallbacks=general_conv_fallbacks
    )

    admin_add_prod_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_prod_entry_cb, pattern="^admin_add_prod_entry_cb$")],
        states={
            ADMIN_ADD_PROD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_prod_name_state)],
            ADMIN_ADD_PROD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_prod_price_state)],
        },
        fallbacks=admin_conv_fallbacks
    )

    admin_manage_prod_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_manage_prod_list_entry_cb, pattern="^admin_manage_prod_list_entry_cb$")],
        states={
            ADMIN_MANAGE_PROD_LIST: [
                CallbackQueryHandler(admin_manage_prod_selected_cb, pattern="^admin_manage_select_prod_\d+$")
            ],
            ADMIN_MANAGE_PROD_OPTIONS: [
                CallbackQueryHandler(admin_manage_edit_price_entry_cb, pattern="^admin_manage_edit_price_entry_cb$"),
                CallbackQueryHandler(admin_manage_toggle_avail_cb, pattern="^admin_manage_toggle_avail_cb_(0|1)$"),
                CallbackQueryHandler(admin_manage_delete_confirm_cb, pattern="^admin_manage_delete_confirm_cb$"),
                CallbackQueryHandler(admin_manage_prod_list_entry_cb, pattern="^admin_manage_prod_list_refresh_cb$") # Back to list from options
            ],
            ADMIN_MANAGE_PROD_EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_manage_edit_price_state)],
            ADMIN_MANAGE_PROD_DELETE_CONFIRM: [
                CallbackQueryHandler(admin_manage_delete_do_cb, pattern="^admin_manage_delete_do_cb$"),
                CallbackQueryHandler(admin_manage_prod_selected_cb, pattern="^admin_manage_select_prod_\d+$") 
            ]
        },
        fallbacks=admin_conv_fallbacks
    )
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command_entry))

    application.add_handler(lang_conv)
    application.add_handler(order_conv)
    application.add_handler(admin_add_prod_conv)
    application.add_handler(admin_manage_prod_conv)
    
    application.add_handler(CallbackQueryHandler(my_orders_direct_cb, pattern="^my_orders_direct_cb$"))
    application.add_handler(CallbackQueryHandler(admin_view_orders_direct_cb, pattern="^admin_view_orders_direct_cb$"))
    application.add_handler(CallbackQueryHandler(admin_shop_list_direct_cb, pattern="^admin_shop_list_direct_cb$"))

    logger.info("Bot starting...")
    application.run_polling()

if __name__ == "__main__": main()
