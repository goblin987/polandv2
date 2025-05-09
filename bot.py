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
    if not os.path.exists(RENDER_DISK_MOUNT_PATH):
        try:
            os.makedirs(RENDER_DISK_MOUNT_PATH)
            logger.info(f"Created RENDER_DISK_MOUNT_PATH at {RENDER_DISK_MOUNT_PATH}")
        except OSError as e:
            # Use logger if it's already configured, otherwise print
            print(f"Error creating RENDER_DISK_MOUNT_PATH {RENDER_DISK_MOUNT_PATH}: {e}. Using local bot.db.")
            DB_FILE_PATH = "bot.db" 
    else:
        DB_FILE_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, "bot.db")
else:
    DB_FILE_PATH = "bot.db" 
DB_NAME = DB_FILE_PATH # Use this global DB_NAME for all sqlite3.connect()

# Enable logging (configure before first use)
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def load_translations():
    global translations
    translations = {} # Reset translations dictionary
    for lang_code in ["en", "lt"]:
        try:
            # Ensure script_dir is correctly determined relative to this file's location
            script_dir = os.path.dirname(os.path.abspath(__file__)) 
            file_path = os.path.join(script_dir, "locales", f"{lang_code}.json")
            with open(file_path, "r", encoding="utf-8") as f:
                translations[lang_code] = json.load(f)
            logger.info(f"Successfully loaded translation file: {file_path}")
        except FileNotFoundError:
            logger.error(f"Translation file for {lang_code}.json not found at {file_path}")
        except json.JSONDecodeError as e: 
            logger.error(f"Error decoding JSON from {lang_code}.json at {file_path}: {e}")
    if not translations.get("en") or not translations.get("lt"):
        logger.error("Essential English or Lithuanian translation files are missing or failed to load.")

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
        # Only format if it's a string and contains placeholders (or if no kwargs, just return as is)
        if isinstance(text_to_return, str) and (("{" in text_to_return and "}" in text_to_return) or not kwargs):
            return text_to_return.format(**kwargs)
        return str(text_to_return) # Ensure it's a string if no formatting needed/possible
    except KeyError as e: 
        logger.warning(f"Missing placeholder {e} for key '{key}' (lang '{lang_code}'). String: '{text_to_return}'. Kwargs: {kwargs}")
        return text_to_return # Return unformatted string (which might contain placeholders)
    except Exception as e:
        logger.error(f"Error formatting string for key '{key}': {e}")
        return key # Fallback to the key itself on other formatting errors


def init_db():
    conn = sqlite3.connect(DB_NAME) 
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

# --- Database Functions (Full versions) ---
def add_product_to_db(name: str, price: float) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO products (name, price_per_kg) VALUES (?, ?)", (name, price))
        conn.commit()
        return True
    except sqlite3.IntegrityError:  # For UNIQUE constraint on name
        logger.warning(f"Attempted to add duplicate product name: {name}")
        return False
    except sqlite3.Error as e:
        logger.error(f"DB error adding product {name}: {e}")
        return False
    finally:
        conn.close()

def get_products_from_db(available_only: bool = True) -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    products = []
    try:
        query = "SELECT id, name, price_per_kg, is_available FROM products"
        if available_only:
            query += " WHERE is_available = 1"
        query += " ORDER BY name"
        cursor.execute(query)
        products = cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error getting products: {e}")
    finally:
        conn.close()
    return products

def get_product_by_id(product_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    product = None
    try:
        cursor.execute("SELECT id, name, price_per_kg, is_available FROM products WHERE id = ?", (product_id,))
        product = cursor.fetchone()
    except sqlite3.Error as e:
        logger.error(f"DB error getting product by ID {product_id}: {e}")
    finally:
        conn.close()
    return product

def update_product_in_db(product_id: int, name: str = None, price: float = None, is_available: int = None) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    success = False
    fields, params = [], []
    if name is not None: fields.append("name = ?"); params.append(name)
    if price is not None: fields.append("price_per_kg = ?"); params.append(price)
    if is_available is not None: fields.append("is_available = ?"); params.append(is_available)
    
    if not fields: conn.close(); return False # No fields to update
    
    params.append(product_id)
    query = f"UPDATE products SET {', '.join(fields)} WHERE id = ?"
    try:
        cursor.execute(query, tuple(params))
        conn.commit()
        success = True
    except sqlite3.Error as e:
        logger.error(f"DB error updating product {product_id}: {e}")
    finally:
        conn.close()
    return success

def delete_product_from_db(product_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    success = False
    try:
        cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        success = True
    except sqlite3.Error as e:
        logger.error(f"DB error deleting product {product_id}: {e}")
    finally:
        conn.close()
    return success

def save_order_to_db(user_id: int, user_name: str, cart: list, total_price: float) -> int | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    order_id = None 
    order_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("BEGIN TRANSACTION")
        cursor.execute("INSERT INTO orders (user_id, user_name, order_date, total_price, status) VALUES (?, ?, ?, ?, ?)", 
                       (user_id, user_name, order_date, total_price, 'pending')) # Default status 'pending'
        order_id = cursor.lastrowid
        for item in cart: 
            cursor.execute("INSERT INTO order_items (order_id, product_id, quantity_kg, price_at_order) VALUES (?, ?, ?, ?)", 
                           (order_id, item['id'], item['quantity'], item['price']))
        conn.commit()
    except sqlite3.Error as e: 
        logger.error(f"Error saving order for user {user_id}: {e}")
        if conn: conn.rollback()
        order_id = None
    finally: 
        if conn: conn.close()
    return order_id

def get_user_orders_from_db(user_id: int) -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    orders = []
    try:
        cursor.execute("SELECT o.id, o.order_date, o.total_price, o.status, group_concat(p.name || ' (' || oi.quantity_kg || 'kg)', ', ') FROM orders o JOIN order_items oi ON o.id = oi.order_id JOIN products p ON oi.product_id = p.id WHERE o.user_id = ? GROUP BY o.id ORDER BY o.order_date DESC", (user_id,))
        orders = cursor.fetchall()
    except sqlite3.Error as e: 
        logger.error(f"DB error getting orders for user {user_id}: {e}")
    finally: 
        conn.close()
    return orders

def get_all_orders_from_db() -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    orders = []
    try:
        cursor.execute("SELECT o.id, o.user_id, o.user_name, o.order_date, o.total_price, o.status, GROUP_CONCAT(p.name || ' (' || oi.quantity_kg || 'kg @ ' || oi.price_at_order || ' EUR)', CHAR(10)) as items_details FROM orders o JOIN order_items oi ON o.id = oi.order_id JOIN products p ON oi.product_id = p.id GROUP BY o.id ORDER BY o.order_date DESC")
        orders = cursor.fetchall()
    except sqlite3.Error as e: 
        logger.error(f"DB error getting all orders: {e}")
    finally: 
        conn.close()
    return orders
    
def get_shopping_list_from_db() -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    shopping_list = []
    try:
        cursor.execute("SELECT p.name, SUM(oi.quantity_kg) as total_quantity FROM order_items oi JOIN products p ON oi.product_id = p.id JOIN orders o ON oi.order_id = o.id WHERE o.status IN ('pending','confirmed') GROUP BY p.name ORDER BY p.name")
        shopping_list = cursor.fetchall()
    except sqlite3.Error as e: 
        logger.error(f"DB error getting shopping list: {e}")
    finally: 
        conn.close()
    return shopping_list

def delete_completed_orders_from_db() -> int:
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); deleted_count = 0
    try:
        cursor.execute("SELECT id FROM orders WHERE status = ?", ('completed',))
        completed_order_ids = [row[0] for row in cursor.fetchall()]
        if not completed_order_ids: conn.close(); return 0
        conn.execute("BEGIN TRANSACTION")
        for order_id in completed_order_ids:
            cursor.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
            cursor.execute("DELETE FROM orders WHERE id = ? AND status = ?", (order_id, 'completed'))
            deleted_count += cursor.rowcount 
        conn.commit()
    except sqlite3.Error as e: 
        logger.error(f"DB error deleting completed orders: {e}")
        if conn: conn.rollback()
        deleted_count = -1 # Indicate error
    finally: 
        if conn: conn.close()
    return deleted_count

def mark_order_as_completed_in_db(order_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    success = False
    try:
        cursor.execute("UPDATE orders SET status = ? WHERE id = ?", ('completed', order_id))
        conn.commit()
        if cursor.rowcount > 0:
            success = True
    except sqlite3.Error as e:
        logger.error(f"DB error marking order {order_id} as completed: {e}")
    finally:
        conn.close()
    return success
# --- End Database Functions ---


# --- Conversation States ---
(SELECT_LANGUAGE_STATE, 
 ORDER_FLOW_BROWSING_PRODUCTS, ORDER_FLOW_SELECTING_QUANTITY, ORDER_FLOW_VIEWING_CART,
 ADMIN_MAIN_PANEL_STATE, 
 ADMIN_ADD_PROD_NAME, ADMIN_ADD_PROD_PRICE, 
 ADMIN_MANAGE_PROD_LIST, ADMIN_MANAGE_PROD_OPTIONS, ADMIN_MANAGE_PROD_EDIT_PRICE, ADMIN_MANAGE_PROD_DELETE_CONFIRM,
 ADMIN_CLEAR_ORDERS_CONFIRM, ADMIN_VIEW_ORDER_DETAILS_FOR_STATUS # New state for viewing specific order to change status
) = range(13) # Range is now 13


# --- Helper: Display Main Menu ---
async def display_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    user = update.effective_user
    if not user: logger.error("display_main_menu called without effective_user"); return 
    user_id = user.id
    if 'language_code' not in context.user_data: context.user_data['language_code'] = await get_user_language(context, user_id)
    kb = [[InlineKeyboardButton(await _(context,"browse_products_button",user_id=user_id),callback_data="order_flow_browse_entry")],[InlineKeyboardButton(await _(context,"view_cart_button",user_id=user_id),callback_data="order_flow_view_cart_direct_entry")],[InlineKeyboardButton(await _(context,"my_orders_button",user_id=user_id),callback_data="my_orders_direct_cb")],[InlineKeyboardButton(await _(context,"set_language_button",user_id=user_id),callback_data="select_language_entry")]]
    welcome = await _(context,"welcome_message",user_id=user_id,user_mention=user.mention_html())
    target = update.callback_query.message if edit_message and update.callback_query else update.message
    try:
        if edit_message and target: await target.edit_text(welcome,reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')
        elif update.message: await update.message.reply_html(welcome,reply_markup=InlineKeyboardMarkup(kb))
        elif user_id: await context.bot.send_message(chat_id=user_id,text=welcome,reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')
    except Exception as e: logger.warning(f"Display main menu error (edit={edit_message}): {e}"); 
    if user_id and not (edit_message and target) and not update.message: await context.bot.send_message(chat_id=user_id,text=welcome,reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')

# --- Start Command & General Back to Main Menu ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    user = update.effective_user; await ensure_user_exists(user.id,user.first_name or "",user.username or "",context) 
    lang=context.user_data.get('language_code'); cart=context.user_data.get('cart')
    # Clear transient data, preserve language and cart
    keys_to_clear = [k for k in context.user_data if k not in ['language_code', 'cart']]
    for k_to_clear in keys_to_clear: context.user_data.pop(k_to_clear, None)
    if lang: context.user_data['language_code']=lang
    elif user: context.user_data['language_code']=await get_user_language(context,user.id)
    if cart is not None: context.user_data['cart']=cart # Preserve cart (even if empty list)
    await display_main_menu(update, context)

async def back_to_main_menu_cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query: await update.callback_query.answer()
    lang=context.user_data.get('language_code'); cart=context.user_data.get('cart')
    transient_keys = [k for k in context.user_data if k not in ['language_code','cart']]
    for t_key in transient_keys: context.user_data.pop(t_key, None)
    if lang: context.user_data['language_code']=lang
    elif update.effective_user: context.user_data['language_code']=await get_user_language(context,update.effective_user.id)
    if cart is not None: context.user_data['cart']=cart
    await display_main_menu(update,context,edit_message=bool(update.callback_query))
    return ConversationHandler.END 

# --- Language Selection Flow ---
async def select_language_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q=update.callback_query;await q.answer();uid=q.from_user.id
    kb=[[InlineKeyboardButton("English 🇬🇧",callback_data="lang_select_en")],[InlineKeyboardButton("Lietuvių 🇱🇹",callback_data="lang_select_lt")],[InlineKeyboardButton(await _(context,"back_button",user_id=uid,default="⬅️ Back"),callback_data="main_menu_direct_cb_ender")]]
    await q.edit_message_text(await _(context,"choose_language",user_id=uid),reply_markup=InlineKeyboardMarkup(kb))
    return SELECT_LANGUAGE_STATE
async def language_selected_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q=update.callback_query;await q.answer();code=q.data.split('_')[-1];uid=q.from_user.id
    context.user_data['language_code']=code;await set_user_language_db(uid,code)
    name="English" if code=="en" else "Lietuvių"
    await q.edit_message_text(await _(context,"language_set_to",user_id=uid,language_name=name))
    await display_main_menu(update,context,edit_message=True);return ConversationHandler.END

# --- User Order Flow ---
async def order_flow_browse_entry(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    logger.info(f"User {update.effective_user.id} entered order_flow_browse_entry CB:{update.callback_query.data}")
    q=update.callback_query;await q.answer();return await order_flow_list_products(update,context,q.from_user.id,True)
async def order_flow_list_products(update:Update,context:ContextTypes.DEFAULT_TYPE,uid:int,edit_message:bool=True)->int:
    q=update.callback_query;prods=get_products_from_db(True);kb,txt=[],""
    if not prods:txt=await _(context,"no_products_available",user_id=uid);kb.append([InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=uid),callback_data="main_menu_direct_cb_ender")])
    else:
        txt=await _(context,"products_title",user_id=uid)
        for pid,name,price,_avail in prods:kb.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR/kg",callback_data=f"order_flow_select_prod_{pid}")])
        kb.append([InlineKeyboardButton(await _(context,"view_cart_button",user_id=uid),callback_data="order_flow_view_cart_state_cb")])
        kb.append([InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=uid),callback_data="main_menu_direct_cb_ender")])
    target=q.message if q else update.message
    try:
        if edit_message and q and target:await target.edit_text(text=txt,reply_markup=InlineKeyboardMarkup(kb))
        elif update.message and target:await target.reply_text(text=txt,reply_markup=InlineKeyboardMarkup(kb))
        elif q and not edit_message and target:await target.reply_text(text=txt,reply_markup=InlineKeyboardMarkup(kb))
        elif uid:await context.bot.send_message(chat_id=uid,text=txt,reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:logger.error(f"Err order_flow_list_products(edit={edit_message}):{e}");if uid:await context.bot.send_message(chat_id=uid,text=txt,reply_markup=InlineKeyboardMarkup(kb))
    return ORDER_FLOW_BROWSING_PRODUCTS
async def order_flow_product_selected(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id
    try:pid=int(q.data.split('_')[-1])
    except:await q.edit_message_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return ORDER_FLOW_BROWSING_PRODUCTS
    prod=get_product_by_id(pid)
    if not prod:await q.edit_message_text(await _(context,"product_not_found",user_id=uid,default="Not found."));return ORDER_FLOW_BROWSING_PRODUCTS
    context.user_data.update({'current_product_id':pid,'current_product_name':prod[1],'current_product_price':prod[2]})
    await q.edit_message_text(await _(context,"product_selected_prompt",user_id=uid,product_name=prod[1]))
    return ORDER_FLOW_SELECTING_QUANTITY
async def order_flow_quantity_typed(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    uid=update.effective_user.id;q_str=update.message.text
    try:qnt=float(q_str);assert qnt>0
    except:await update.message.reply_text(await _(context,"invalid_quantity_prompt",user_id=uid));return ORDER_FLOW_SELECTING_QUANTITY
    pid,pname,pprice=context.user_data.get('current_product_id'),context.user_data.get('current_product_name'),context.user_data.get('current_product_price')
    if not all([pid is not None,pname is not None,pprice is not None]):await update.message.reply_text(await _(context,"generic_error_message",user_id=uid,default="Error. Try add again."));return await order_flow_list_products(update,context,uid,False)
    cart=context.user_data.setdefault('cart',[])
    found=any(i['id']==pid and(i.update({'quantity':i['quantity']+qnt})or True)for i in cart)
    if not found:cart.append({'id':pid,'name':pname,'price':pprice,'quantity':qnt})
    await update.message.reply_text(await _(context,"item_added_to_cart",user_id=uid,quantity=qnt,product_name=pname))
    kb=[[InlineKeyboardButton(await _(context,"add_more_products_button",user_id=uid),callback_data="order_flow_browse_return_cb")],[InlineKeyboardButton(await _(context,"view_cart_button",user_id=uid),callback_data="order_flow_view_cart_state_cb")],[InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=uid),callback_data="main_menu_direct_cb_ender")]]
    await update.message.reply_text(await _(context,"what_next_prompt",user_id=uid),reply_markup=InlineKeyboardMarkup(kb))
    return ORDER_FLOW_BROWSING_PRODUCTS
async def order_flow_view_cart_state_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id;return await order_flow_display_cart(update,context,uid,True)
async def order_flow_view_cart_direct_entry(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id;context.user_data.setdefault('cart',[])
    await order_flow_display_cart(update,context,uid,True);return ORDER_FLOW_VIEWING_CART
async def order_flow_display_cart(update:Update,context:ContextTypes.DEFAULT_TYPE,uid:int,edit:bool)->int:
    cart=context.user_data.get('cart',[]);target=update.callback_query.message if edit and update.callback_query else update.message
    txt,kb_buttons="",[]
    if not cart:txt=await _(context,"cart_empty",user_id=uid);kb_buttons.append([InlineKeyboardButton(await _(context,"browse_products_button",user_id=uid),callback_data="order_flow_browse_return_cb")])
    else:
        txt=await _(context,"your_cart_title",user_id=uid)+"\n";total=0
        for i,item in enumerate(cart):subtotal=item['price']*item['quantity'];total+=subtotal;txt+=f"{i+1}. {item['name']} - {item['quantity']} kg x {item['price']:.2f} EUR = {subtotal:.2f} EUR\n";kb_buttons.append([InlineKeyboardButton(await _(context,"remove_item_button",user_id=uid,item_index=i+1),callback_data=f"order_flow_remove_item_{i}")])
        txt+="\n"+await _(context,"cart_total",user_id=uid,total_price=total)
        kb_buttons.append([InlineKeyboardButton(await _(context,"checkout_button",user_id=uid),callback_data="order_flow_checkout_cb")])
        kb_buttons.append([InlineKeyboardButton(await _(context,"add_more_products_button",user_id=uid),callback_data="order_flow_browse_return_cb")])
    kb_buttons.append([InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=uid),callback_data="main_menu_direct_cb_ender")])
    try:
        if edit and target:await target.edit_text(text=txt,reply_markup=InlineKeyboardMarkup(kb_buttons))
        elif update.message and target:await target.reply_text(text=txt,reply_markup=InlineKeyboardMarkup(kb_buttons))
        elif uid:await context.bot.send_message(chat_id=uid,text=txt,reply_markup=InlineKeyboardMarkup(kb_buttons))
    except Exception as e:logger.error(f"Err display_cart:{e}");if uid:await context.bot.send_message(chat_id=uid,text=txt,reply_markup=InlineKeyboardMarkup(kb_buttons))
    return ORDER_FLOW_VIEWING_CART
async def order_flow_remove_item_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id
    try:idx=int(q.data.split('_')[-1])
    except:await q.message.reply_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return ORDER_FLOW_VIEWING_CART
    cart=context.user_data.get('cart',[])
    if 0<=idx<len(cart):removed=cart.pop(idx);await q.message.reply_text(await _(context,"item_removed_from_cart",user_id=uid,item_name=removed['name']))
    else:await q.message.reply_text(await _(context,"invalid_item_to_remove",user_id=uid))
    return await order_flow_display_cart(update,context,uid,True)
async def order_flow_checkout_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();user=q.from_user;uid=user.id;cart=context.user_data.get('cart',[])
    if not cart:await q.edit_message_text(await _(context,"cart_empty",user_id=uid));return ORDER_FLOW_VIEWING_CART
    uname=user.full_name or "N/A";total=sum(i['price']*i['quantity'] for i in cart);oid=save_order_to_db(uid,uname,cart,total)
    admin_lang_uid=ADMIN_IDS[0] if ADMIN_IDS else None
    if oid:
        await q.edit_message_text(await _(context,"order_placed_success",user_id=uid,order_id=oid,total_price=total))
        admin_title=await _(context,"admin_new_order_notification_title",user_id=admin_lang_uid,order_id=oid,default=f"🔔 New Order #{oid}")
        admin_msg=f"{admin_title}\n"+await _(context,"admin_order_from",user_id=admin_lang_uid,name=uname,username=(user.username or ""),customer_id=uid,default=f"From:{uname}...")+"\n\n"+await _(context,"admin_order_items_header",user_id=admin_lang_uid,default="Items:")+"\n------------------------------------\n"
        item_lines=[await _(context,"admin_order_item_line_format",user_id=admin_lang_uid,index=i+1,item_name=c['name'],quantity=c['quantity'],price_per_kg=c['price'],item_subtotal=(c['price']*c['quantity']),default=f"{i+1}. {c['name']}: {c['quantity']:.2f} kg x {c['price']:.2f} EUR/kg = {(c['price']*c['quantity']):.2f} EUR")for i,c in enumerate(cart)]
        admin_msg+="\n".join(item_lines)+"\n------------------------------------\n"+await _(context,"admin_order_grand_total",user_id=admin_lang_uid,total_price=total,default=f"Total:{total:.2f} EUR")
        if ADMIN_IDS:
            for admin_id in ADMIN_IDS:
                try:
                    if len(admin_msg)>4096:
                        for i_part in range(0,len(admin_msg),4096):await context.bot.send_message(chat_id=admin_id,text=admin_msg[i_part:i_part+4096])
                    else:await context.bot.send_message(chat_id=admin_id,text=admin_msg)
                except Exception as e:logger.error(f"Notify admin {admin_id} err:{e}")
        lang=context.user_data.get('language_code');keys_pop=['cart','current_product_id','current_product_name','current_product_price'];[context.user_data.pop(k,None)for k in keys_pop]
        if lang:context.user_data['language_code']=lang
        await display_main_menu(update,context,False)
    else:
        await q.edit_message_text(await _(context,"order_placed_error",user_id=uid))
        kb=[[InlineKeyboardButton(await _(context,"view_cart_button",user_id=uid),callback_data="order_flow_view_cart_state_cb")],[InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=uid),callback_data="main_menu_direct_cb_ender")]]
        next_txt=await _(context,"what_next_prompt",user_id=uid,default="What next?");
        try:await q.message.reply_text(next_txt,reply_markup=InlineKeyboardMarkup(kb))
        except AttributeError: await context.bot.send_message(chat_id=uid,text=next_txt,reply_markup=InlineKeyboardMarkup(kb)) # If q.message is None
        return ORDER_FLOW_VIEWING_CART
    return ConversationHandler.END
async def my_orders_direct_cb(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query;await q.answer();uid=q.from_user.id;orders=get_user_orders_from_db(uid)
    txt=await _(context,"my_orders_title",user_id=uid,default="Orders:")+"\n\n" if orders else await _(context,"no_orders_yet",user_id=uid)
    if orders:
        for oid,date,total,status,items in orders:txt+=await _(context,"order_details_format",user_id=uid,order_id=oid,date=date,status=status.capitalize(),total=total,items=items,default="Order...")
    kb=[[InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=uid),callback_data="main_menu_direct_cb_ender")]]
    await q.edit_message_text(text=txt,reply_markup=InlineKeyboardMarkup(kb))

# --- Admin Panel and Flows ---
async def display_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False) -> int:
    user = update.effective_user; 
    if not user: logger.error("display_admin_panel: effective_user is None"); return ConversationHandler.END
    user_id = user.id
    if not (ADMIN_IDS and user_id in ADMIN_IDS): 
        unauth_text=await _(context,"admin_unauthorized",user_id=user_id);target=update.callback_query.message if edit_message and update.callback_query else update.message
        if edit_message and target:await target.edit_text(unauth_text)
        elif update.message:await update.message.reply_text(unauth_text)
        elif user_id:await context.bot.send_message(chat_id=user_id,text=unauth_text)
        return ConversationHandler.END 
    context.chat_data['user_id_for_translation']=user_id 
    kb=[[InlineKeyboardButton(await _(context,"admin_add_product_button",user_id=user_id),callback_data="admin_add_prod_entry_cb")],[InlineKeyboardButton(await _(context,"admin_manage_products_button",user_id=user_id),callback_data="admin_manage_prod_list_entry_cb")],[InlineKeyboardButton(await _(context,"admin_view_orders_button",user_id=user_id),callback_data="admin_view_orders_direct_cb")],[InlineKeyboardButton(await _(context,"admin_shopping_list_button",user_id=user_id),callback_data="admin_shop_list_direct_cb")],[InlineKeyboardButton(await _(context,"admin_clear_orders_button",user_id=user_id,default="🧹 Clear Orders"),callback_data="admin_clear_orders_entry_cb")],[InlineKeyboardButton(await _(context,"admin_exit_button",user_id=user_id),callback_data="main_menu_direct_cb_ender")]]
    title=await _(context,"admin_panel_title",user_id=user_id);target=update.callback_query.message if edit_message and update.callback_query else update.message
    try:
        if edit_message and target:await target.edit_text(title,reply_markup=InlineKeyboardMarkup(kb))
        elif update.message:await update.message.reply_text(title,reply_markup=InlineKeyboardMarkup(kb))
        elif user_id:await context.bot.send_message(chat_id=user_id,text=title,reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:logger.warning(f"Disp admin panel err:{e}");if user_id and not(edit_message and target)and not update.message:await context.bot.send_message(chat_id=user_id,text=title,reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MAIN_PANEL_STATE

async def admin_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await display_admin_panel(update,context)
async def admin_panel_return_direct_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    q=update.callback_query;logger.info(f"admin_panel_return_direct_cb by {q.from_user.id if q else 'Unk'} data:{q.data if q else 'N/A'}")
    if q:await q.answer()
    context.user_data.pop('editing_pid',None);context.user_data.pop('new_pname',None)
    return_state=await display_admin_panel(update,context,True);logger.info(f"display_admin_panel ret state:{return_state}");return return_state

# Admin Add Product
async def admin_add_prod_entry_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id;await q.edit_message_text(await _(context,"admin_enter_product_name",user_id=uid));return ADMIN_ADD_PROD_NAME
async def admin_add_prod_name_state(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    uid=update.effective_user.id;pname=update.message.text;context.user_data['new_pname']=pname;await update.message.reply_text(await _(context,"admin_enter_product_price",user_id=uid,product_name=pname));return ADMIN_ADD_PROD_PRICE
async def admin_add_prod_price_state(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    uid=update.effective_user.id;name=context.user_data.get('new_pname')
    try:price=float(update.message.text);assert price>0
    except:await update.message.reply_text(await _(context,"admin_invalid_price",user_id=uid));return ADMIN_ADD_PROD_PRICE
    if not name:await update.message.reply_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return await display_admin_panel(update,context,False)
    fmt_kwargs={'user_id':uid,'product_name':name};msg_key="admin_product_added" if add_product_to_db(name,price) else "admin_product_add_failed"
    if msg_key=="admin_product_added":fmt_kwargs['price']=price
    await update.message.reply_text(await _(context,msg_key,**fmt_kwargs))
    if 'new_pname' in context.user_data:del context.user_data['new_pname']
    await display_admin_panel(update,context,False);return ConversationHandler.END

# Admin Manage Products
async def admin_manage_prod_list_entry_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id;context.user_data.pop('editing_pid',None);prods=get_products_from_db(False);kb,txt=[],""
    if not prods:txt=await _(context,"admin_no_products_to_manage",user_id=uid);kb.append([InlineKeyboardButton(await _(context,"admin_back_to_admin_panel_button",user_id=uid),callback_data="admin_panel_return_direct_cb")])
    else:
        txt=await _(context,"admin_select_product_to_manage",user_id=uid)
        for pid,name,price,avail in prods:stat_key="admin_status_available" if avail else "admin_status_unavailable";stat=await _(context,stat_key,user_id=uid,default="Avail" if avail else "Unavail");kb.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR ({stat})",callback_data=f"admin_manage_select_prod_{pid}")])
        kb.append([InlineKeyboardButton(await _(context,"admin_back_to_admin_panel_button",user_id=uid),callback_data="admin_panel_return_direct_cb")])
    await q.edit_message_text(text=txt,reply_markup=InlineKeyboardMarkup(kb));return ADMIN_MANAGE_PROD_LIST
async def admin_manage_prod_selected_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id
    try:pid=int(q.data.split('_')[-1])
    except:await q.edit_message_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return ADMIN_MANAGE_PROD_LIST
    prod=get_product_by_id(pid)
    if not prod:await q.edit_message_text(await _(context,"product_not_found",user_id=uid,default="Not found."));return ADMIN_MANAGE_PROD_LIST
    context.user_data['editing_pid']=pid;pname,pprice,pavail=prod[1],prod[2],prod[3];avail_key="admin_set_unavailable_button" if pavail else "admin_set_available_button"
    kb=[[InlineKeyboardButton(await _(context,"admin_change_price_button",user_id=uid,price=pprice),callback_data="admin_manage_edit_price_entry_cb")],[InlineKeyboardButton(await _(context,avail_key,user_id=uid),callback_data=f"admin_manage_toggle_avail_cb_{1-pavail}")],[InlineKeyboardButton(await _(context,"admin_delete_product_button",user_id=uid),callback_data="admin_manage_delete_confirm_cb")],[InlineKeyboardButton(await _(context,"admin_back_to_product_list_button",user_id=uid),callback_data="admin_manage_prod_list_refresh_cb")]]
    await q.edit_message_text(await _(context,"admin_managing_product",user_id=uid,product_name=pname),reply_markup=InlineKeyboardMarkup(kb));return ADMIN_MANAGE_PROD_OPTIONS
async def admin_manage_edit_price_entry_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id;edit_pid=context.user_data.get('editing_pid')
    if not edit_pid:await q.edit_message_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return ADMIN_MANAGE_PROD_LIST
    prod=get_product_by_id(edit_pid)
    if not prod:await q.edit_message_text(await _(context,"product_not_found",user_id=uid,default="Not found."));return ADMIN_MANAGE_PROD_LIST
    await q.edit_message_text(await _(context,"admin_enter_new_price",user_id=uid,product_name=prod[1],current_price=prod[2]));return ADMIN_MANAGE_PROD_EDIT_PRICE
async def admin_manage_edit_price_state(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    uid=update.effective_user.id;price_str=update.message.text;edit_pid=context.user_data.get('editing_pid')
    if not edit_pid:await update.message.reply_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return await admin_panel_return_direct_cb(update,context)
    try:price_new=float(price_str);assert price_new>0
    except:await update.message.reply_text(await _(context,"admin_invalid_price",user_id=uid));return ADMIN_MANAGE_PROD_EDIT_PRICE
    msg_key="admin_price_updated" if update_product_in_db(edit_pid,price=price_new) else "admin_price_update_failed"
    await update.message.reply_text(await _(context,msg_key,user_id=uid,product_id=edit_pid))
    class MQ:from_user=update.effective_user;message=update.message;data=f"admin_manage_select_prod_{edit_pid}";async def answer():pass
    mq=MQ();m_upd=Update(0,callback_query=mq);m_upd.effective_user=update.effective_user
    return await admin_manage_prod_selected_cb(m_upd,context)
async def admin_manage_toggle_avail_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id;edit_pid=context.user_data.get('editing_pid')
    if not edit_pid:await q.edit_message_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return ADMIN_MANAGE_PROD_LIST
    try:new_avail=int(q.data.split('_')[-1])
    except:await q.edit_message_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return ADMIN_MANAGE_PROD_OPTIONS
    ok=update_product_in_db(edit_pid,is_available=new_avail);st_key="admin_status_available_text" if new_avail else "admin_status_unavailable_text";st_txt=await _(context,st_key,user_id=uid,default="avail" if new_avail else "unavail");msg_key="admin_product_set_status" if ok else "admin_status_update_failed"
    await q.edit_message_text(await _(context,msg_key,user_id=uid,product_id=edit_pid,status_text=st_txt));q.data=f"admin_manage_select_prod_{edit_pid}"
    return await admin_manage_prod_selected_cb(update,context)
async def admin_manage_delete_confirm_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id;edit_pid=context.user_data.get('editing_pid')
    if not edit_pid:await q.edit_message_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return ADMIN_MANAGE_PROD_LIST
    prod=get_product_by_id(edit_pid)
    if not prod:await q.edit_message_text(await _(context,"product_not_found",user_id=uid,default="Not found."));return ADMIN_MANAGE_PROD_LIST
    kb=[[InlineKeyboardButton(await _(context,"admin_confirm_delete_yes_button",user_id=uid,product_name=prod[1]),callback_data="admin_manage_delete_do_cb")],[InlineKeyboardButton(await _(context,"admin_confirm_delete_no_button",user_id=uid),callback_data=f"admin_manage_select_prod_{edit_pid}")]]
    await q.edit_message_text(await _(context,"admin_confirm_delete_prompt",user_id=uid,product_name=prod[1]),reply_markup=InlineKeyboardMarkup(kb));return ADMIN_MANAGE_PROD_DELETE_CONFIRM
async def admin_manage_delete_do_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id;edit_pid=context.user_data.get('editing_pid')
    if not edit_pid:await q.edit_message_text(await _(context,"generic_error_message",user_id=uid,default="Error."));return ADMIN_MANAGE_PROD_LIST
    msg_key="admin_product_deleted" if delete_product_from_db(edit_pid) else "admin_product_delete_failed"
    await q.edit_message_text(await _(context,msg_key,user_id=uid,product_id=edit_pid))
    if 'editing_pid' in context.user_data:del context.user_data['editing_pid']
    return await admin_manage_prod_list_entry_cb(update,context)

# Admin Clear Orders Flow
async def admin_clear_completed_orders_entry_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id
    confirm_txt=await _(context,"admin_clear_orders_confirm_prompt",user_id=uid,default="Sure to delete COMPLETED orders?");yes_txt=await _(context,"admin_clear_orders_yes_button",user_id=uid,default="YES, Delete");no_txt=await _(context,"admin_clear_orders_no_button",user_id=uid,default="NO, Cancel")
    kb=[[InlineKeyboardButton(yes_txt,callback_data="admin_clear_orders_do_confirm")],[InlineKeyboardButton(no_txt,callback_data="admin_panel_return_direct_cb")]] # No on confirm goes to admin panel
    await q.edit_message_text(text=confirm_txt,reply_markup=InlineKeyboardMarkup(kb));return ADMIN_CLEAR_ORDERS_CONFIRM
async def admin_clear_orders_do_confirm_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query;await q.answer();uid=q.from_user.id
    if not(ADMIN_IDS and uid in ADMIN_IDS):await q.edit_message_text(await _(context,"admin_unauthorized",user_id=uid));return ConversationHandler.END
    deleted_count=delete_completed_orders_from_db()
    if deleted_count>0:msg=await _(context,"admin_orders_cleared_success",user_id=uid,count=deleted_count,default=f"{deleted_count} orders cleared.")
    elif deleted_count==0:msg=await _(context,"admin_orders_cleared_none",user_id=uid,default="No completed orders.")
    else:msg=await _(context,"admin_orders_cleared_error",user_id=uid,default="Error clearing.")
    await q.edit_message_text(text=msg) # Show result
    # After showing result, display admin panel again. We need to make display_admin_panel the return target.
    # The ConversationHandler will end, and then we need to be back at the admin panel.
    # This call will edit the message again to show the admin panel.
    return await display_admin_panel(update,context,True)


# Direct Admin Actions
async def admin_view_orders_direct_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query;await q.answer();uid=q.from_user.id 
    orders=get_all_orders_from_db()
    text = await _(context,"admin_all_orders_title",user_id=uid, default="All Orders:") if orders else await _(context,"admin_no_orders_found",user_id=uid)
    if orders:
        for oid, cust_id_db, uname, date_val, total_val, status_val, items_val in orders: 
            text+=await _(context,"admin_order_details_format",user_id=uid,order_id=oid,user_name=uname,customer_id=cust_id_db,date=date_val,total=total_val,status=status_val.capitalize(),items=items_val, default="Order...")
    if len(text)>4000:text=text[:3950]+"\n...(truncated)" # Ensure space for buttons
    kb=[[InlineKeyboardButton(await _(context,"admin_back_to_admin_panel_button",user_id=uid),callback_data="admin_panel_return_direct_cb")]]
    try: await q.edit_message_text(text=text,reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: logger.error(f"Error admin_view_orders: {e}"); await q.message.reply_text("Error displaying orders.")


async def admin_shop_list_direct_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query;await q.answer();uid=q.from_user.id
    slist=get_shopping_list_from_db()
    text=await _(context,"admin_shopping_list_title",user_id=uid, default="Shopping List:") if slist else await _(context,"admin_shopping_list_empty",user_id=uid)
    if slist:
        for name,qty in slist: text+=await _(context,"admin_shopping_list_item_format",user_id=uid,name=name,total_quantity=qty, default=f"- {name}:{qty}kg\n")
    kb=[[InlineKeyboardButton(await _(context,"admin_back_to_admin_panel_button",user_id=uid),callback_data="admin_panel_return_direct_cb")]]
    try: await q.edit_message_text(text=text,reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: logger.error(f"Error admin_shop_list: {e}"); await q.message.reply_text("Error displaying shopping list.")


# General Cancel Handler
async def general_cancel_command_handler(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    uid=update.effective_user.id;edit,target_msg=False,update.message
    if update.callback_query:await update.callback_query.answer();target_msg=update.callback_query.message;edit=True
    cancel_txt=await _(context,"action_cancelled",user_id=uid,default="Cancelled.")
    try:
        if edit and target_msg:await target_msg.edit_text(cancel_txt)
        elif target_msg:await target_msg.reply_text(cancel_txt,reply_markup=ReplyKeyboardRemove())
        elif update.effective_chat:await context.bot.send_message(chat_id=update.effective_chat.id,text=cancel_txt)
    except Exception as e:logger.warning(f"Cancel err:{e}");if update.effective_chat and not(edit and target_msg):await context.bot.send_message(chat_id=update.effective_chat.id,text=cancel_txt)
    lang=context.user_data.get('language_code');cart_data=context.user_data.get('cart') 
    keys_pop=['current_product_id','current_product_name','current_product_price','new_pname','editing_pid']
    for k in keys_pop:context.user_data.pop(k,None)
    if lang:context.user_data['language_code']=lang
    if cart_data is not None: context.user_data['cart'] = cart_data # Preserve cart on general cancel
    
    if ADMIN_IDS and uid in ADMIN_IDS:await display_admin_panel(update,context,edit_message=edit if target_msg else False)
    else:await display_main_menu(update,context,edit_message=edit if target_msg else False)
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

    # Common fallbacks for user conversations (ends conv, goes to main menu)
    user_conv_fallbacks = [
        CallbackQueryHandler(back_to_main_menu_cb_handler, pattern="^main_menu_direct_cb_ender$"),
        CommandHandler("cancel", general_cancel_command_handler),
        CommandHandler("start", start_command) 
    ]
    # Common fallbacks for admin conversations (ends conv, goes to admin panel)
    admin_conv_fallbacks = [
        CallbackQueryHandler(admin_panel_return_direct_cb, pattern="^admin_panel_return_direct_cb$"), 
        CommandHandler("cancel", general_cancel_command_handler), 
        CommandHandler("admin", admin_command_entry) 
    ]

    lang_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_language_entry, pattern="^select_language_entry$")],
        states={SELECT_LANGUAGE_STATE: [CallbackQueryHandler(language_selected_state, pattern="^lang_select_(en|lt)$")]},
        fallbacks=user_conv_fallbacks # Language selection is a user flow
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
        fallbacks=user_conv_fallbacks
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
                CallbackQueryHandler(admin_manage_prod_list_entry_cb, pattern="^admin_manage_prod_list_refresh_cb$") 
            ],
            ADMIN_MANAGE_PROD_EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_manage_edit_price_state)],
            ADMIN_MANAGE_PROD_DELETE_CONFIRM: [
                CallbackQueryHandler(admin_manage_delete_do_cb, pattern="^admin_manage_delete_do_cb$"),
                CallbackQueryHandler(admin_manage_prod_selected_cb, pattern="^admin_manage_select_prod_\d+$") 
            ]
        },
        fallbacks=admin_conv_fallbacks
    )

    admin_clear_orders_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_clear_completed_orders_entry_cb, pattern="^admin_clear_orders_entry_cb$")],
        states={
            ADMIN_CLEAR_ORDERS_CONFIRM: [
                CallbackQueryHandler(admin_clear_orders_do_confirm_cb, pattern="^admin_clear_orders_do_confirm$")
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
    application.add_handler(admin_clear_orders_conv) # Add the new conversation handler
    
    application.add_handler(CallbackQueryHandler(my_orders_direct_cb, pattern="^my_orders_direct_cb$"))
    application.add_handler(CallbackQueryHandler(admin_view_orders_direct_cb, pattern="^admin_view_orders_direct_cb$"))
    application.add_handler(CallbackQueryHandler(admin_shop_list_direct_cb, pattern="^admin_shop_list_direct_cb$"))

    logger.info("Bot starting...")
    application.run_polling()

if __name__ == "__main__": main()
