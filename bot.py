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

translations = {}
ADMIN_IDS = [] 

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
        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON from {lang_code}.json at {file_path}")
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
    
    # Fallback logic: try user's lang, then default lang, then English, then key itself with default from kwargs
    text_to_return = translations.get(lang_code, {}).get(key)
    if text_to_return is None and lang_code != DEFAULT_LANGUAGE:
        text_to_return = translations.get(DEFAULT_LANGUAGE, {}).get(key)
    if text_to_return is None and lang_code != "en" and DEFAULT_LANGUAGE != "en":
        text_to_return = translations.get("en", {}).get(key)
    if text_to_return is None:
        default_text = kwargs.pop("default", key) # Pop default so it's not passed to .format
        logger.warning(f"Translation key '{key}' not found. Using default: '{default_text}'")
        text_to_return = default_text
    
    try:
        # Only format if it's a string and contains placeholders (or if no kwargs, just return)
        if isinstance(text_to_return, str) and ("{" in text_to_return and "}" in text_to_return or not kwargs):
            return text_to_return.format(**kwargs)
        return str(text_to_return) # Ensure it's a string if no formatting needed
    except KeyError as e: 
        logger.warning(f"Missing placeholder {e} for key '{key}' (lang '{lang_code}'). String: '{text_to_return}'. Kwargs: {kwargs}")
        return text_to_return 
    except Exception as e:
        logger.error(f"Error formatting string for key '{key}': {e}")
        return key

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
DB_NAME = "bot.db"

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

# --- Database Functions (shortened for brevity in example, use full versions) ---
def add_product_to_db(name, price):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor()
    try: cursor.execute("INSERT INTO products (name, price_per_kg) VALUES (?,?)",(name,price)); conn.commit(); return True
    except sqlite3.IntegrityError: return False
    finally: conn.close()
def get_products_from_db(available_only=True):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); products=[]
    q="SELECT id, name, price_per_kg, is_available FROM products"
    if available_only: q+=" WHERE is_available = 1"
    q+=" ORDER BY name"; cursor.execute(q); products=cursor.fetchall(); conn.close(); return products
def get_product_by_id(pid):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); prod=None
    cursor.execute("SELECT id,name,price_per_kg,is_available FROM products WHERE id=?",(pid,)); prod=cursor.fetchone(); conn.close(); return prod
def update_product_in_db(pid,name=None,price=None,is_available=None):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); success=False; fields,params=[],[]
    if name: fields.append("name=?"); params.append(name)
    if price: fields.append("price_per_kg=?"); params.append(price)
    if is_available is not None: fields.append("is_available=?"); params.append(is_available)
    if not fields: conn.close(); return False
    params.append(pid); q=f"UPDATE products SET {','.join(fields)} WHERE id=?"; cursor.execute(q,tuple(params)); conn.commit(); success=True; conn.close(); return success
def delete_product_from_db(pid):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); success=False
    try: cursor.execute("DELETE FROM products WHERE id=?",(pid,)); conn.commit(); success=True
    except: pass
    finally: conn.close(); return success
def save_order_to_db(uid,uname,cart,total):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); oid=None; date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute("INSERT INTO orders (user_id,user_name,order_date,total_price) VALUES (?,?,?,?)",(uid,uname,date,total)); oid=cursor.lastrowid
        for item in cart: cursor.execute("INSERT INTO order_items (order_id,product_id,quantity_kg,price_at_order) VALUES (?,?,?,?)",(oid,item['id'],item['quantity'],item['price']))
        conn.commit()
    except: oid=None
    finally: conn.close(); return oid
def get_user_orders_from_db(uid):
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); orders=[]
    cursor.execute("SELECT o.id,o.order_date,o.total_price,o.status,group_concat(p.name||' ('||oi.quantity_kg||'kg)',', ') FROM orders o JOIN order_items oi ON o.id=oi.order_id JOIN products p ON oi.product_id=p.id WHERE o.user_id=? GROUP BY o.id ORDER BY o.order_date DESC",(uid,)); orders=cursor.fetchall(); conn.close(); return orders
def get_all_orders_from_db():
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); orders=[]
    cursor.execute("SELECT o.id,o.user_id,o.user_name,o.order_date,o.total_price,o.status,GROUP_CONCAT(p.name||' ('||oi.quantity_kg||'kg @ '||oi.price_at_order||' EUR)',CHAR(10)) as items_details FROM orders o JOIN order_items oi ON o.id=oi.order_id JOIN products p ON oi.product_id=p.id GROUP BY o.id ORDER BY o.order_date DESC"); orders=cursor.fetchall(); conn.close(); return orders
def get_shopping_list_from_db():
    conn=sqlite3.connect(DB_NAME); cursor=conn.cursor(); slist=[]
    cursor.execute("SELECT p.name,SUM(oi.quantity_kg) as total_quantity FROM order_items oi JOIN products p ON oi.product_id=p.id JOIN orders o ON oi.order_id=o.id WHERE o.status IN ('pending','confirmed') GROUP BY p.name ORDER BY p.name"); slist=cursor.fetchall(); conn.close(); return slist
# --- End Database Functions ---


# --- Conversation States ---
(MAIN_MENU_STATE, # General main menu state
 SELECT_LANGUAGE_STATE, # For language selection conversation
 # User Order Flow States
 ORDER_FLOW_BROWSING_PRODUCTS, ORDER_FLOW_SELECTING_QUANTITY, ORDER_FLOW_VIEWING_CART,
 # Admin Panel States
 ADMIN_MAIN_PANEL_STATE, # Main admin panel
 ADMIN_ADD_PROD_NAME, ADMIN_ADD_PROD_PRICE, # Add product flow
 ADMIN_MANAGE_PROD_LIST, ADMIN_MANAGE_PROD_OPTIONS, ADMIN_MANAGE_PROD_EDIT_PRICE, ADMIN_MANAGE_PROD_DELETE_CONFIRM # Manage products flow
) = range(12)


async def display_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    user = update.effective_user
    user_id = user.id
    if 'language_code' not in context.user_data:
        context.user_data['language_code'] = await get_user_language(context, user_id)

    keyboard = [
        [InlineKeyboardButton(await _(context, "browse_products_button", user_id=user_id), callback_data="order_flow_browse_entry")],
        [InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="order_flow_view_cart_direct")], 
        [InlineKeyboardButton(await _(context, "my_orders_button", user_id=user_id), callback_data="my_orders_direct")], 
        [InlineKeyboardButton(await _(context, "set_language_button", user_id=user_id), callback_data="select_language_entry")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = await _(context, "welcome_message", user_id=user_id, user_mention=user.mention_html())
    
    target_message = update.callback_query.message if edit_message and update.callback_query else update.message
    try:
        if edit_message and target_message:
            await target_message.edit_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await update.message.reply_html(welcome_text, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Display main menu error (edit={edit_message}): {e}. Sending new message.")
        await update.message.reply_html(welcome_text, reply_markup=reply_markup) # Fallback
    return MAIN_MENU_STATE


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    await ensure_user_exists(user.id, user.first_name or "", user.username or "", context) 
    lang = context.user_data.get('language_code')
    # Minimal clear, or ensure conversation endings handle their own data
    context.user_data.clear() # Start fresh, but preserve language
    if lang: context.user_data['language_code'] = lang
    else: context.user_data['language_code'] = await get_user_language(context, user.id)
    return await display_main_menu(update, context)

async def back_to_main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ends current conversation and displays main menu."""
    if update.callback_query: await update.callback_query.answer()
    # Clear conversation-specific data if necessary, e.g., context.user_data.pop('cart', None)
    return await display_main_menu(update, context, edit_message=bool(update.callback_query))


async def select_language_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    keyboard = [
        [InlineKeyboardButton("English 🇬🇧", callback_data="lang_en")],
        [InlineKeyboardButton("Lietuvių 🇱🇹", callback_data="lang_lt")],
        [InlineKeyboardButton(await _(context, "back_button", user_id=user_id, default="⬅️ Back"), callback_data="main_menu_direct_cb")]
    ]
    await query.edit_message_text(await _(context, "choose_language", user_id=user_id), reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_LANGUAGE_STATE

async def language_selected_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); lang_code = query.data.split('_')[1]; user_id = query.from_user.id
    context.user_data['language_code'] = lang_code; await set_user_language_db(user_id, lang_code)
    lang_name = "English" if lang_code == "en" else "Lietuvių"
    await query.edit_message_text(await _(context, "language_set_to", user_id=user_id, language_name=lang_name))
    await display_main_menu(update, context, edit_message=True) # Show main menu after lang change
    return ConversationHandler.END


# --- User Order Flow ---
async def order_flow_browse_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    return await order_flow_list_products(update, context, query.from_user.id)

async def order_flow_list_products(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    query = update.callback_query # Assumed to be called from a query context
    products = get_products_from_db(available_only=True)
    keyboard = []
    if not products:
        no_prod_text = await _(context, "no_products_available", user_id=user_id)
        keyboard.append([InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="main_menu_direct_cb")])
        await query.edit_message_text(text=no_prod_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return ORDER_FLOW_BROWSING_PRODUCTS # Stay in a state that can handle the back button
    
    for pid, name, price, _avail in products: 
        keyboard.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR/kg", callback_data=f"order_flow_select_prod_{pid}")])
    keyboard.append([InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="order_flow_view_cart_state")])
    keyboard.append([InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="main_menu_direct_cb")])
    await query.edit_message_text(await _(context, "products_title", user_id=user_id), reply_markup=InlineKeyboardMarkup(keyboard))
    return ORDER_FLOW_BROWSING_PRODUCTS

async def order_flow_product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    try: product_id = int(query.data.split('_')[-1])
    except: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id)); return ORDER_FLOW_BROWSING_PRODUCTS
    product = get_product_by_id(product_id)
    if not product: await query.edit_message_text(await _(context,"product_not_found",user_id=user_id)); return ORDER_FLOW_BROWSING_PRODUCTS
    context.user_data.update({'pid':product_id, 'pname':product[1], 'pprice':product[2]})
    await query.edit_message_text(await _(context,"product_selected_prompt",user_id=user_id,product_name=product[1]))
    return ORDER_FLOW_SELECTING_QUANTITY

async def order_flow_quantity_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; quantity_str = update.message.text
    try: quantity = float(quantity_str); assert quantity > 0
    except: await update.message.reply_text(await _(context,"invalid_quantity_prompt",user_id=user_id)); return ORDER_FLOW_SELECTING_QUANTITY
    
    pid,pname,pprice = context.user_data.get('pid'),context.user_data.get('pname'),context.user_data.get('pprice')
    if not all([pid,pname,pprice is not None]): # Error if product context lost
        await update.message.reply_text(await _(context,"generic_error_message",user_id=user_id)); return await back_to_main_menu_handler(update,context)

    cart = context.user_data.setdefault('cart', [])
    found = False
    for item in cart:
        if item['id'] == pid: item['quantity'] += quantity; found = True; break
    if not found: cart.append({'id':pid,'name':pname,'price':pprice,'quantity':quantity})
    
    await update.message.reply_text(await _(context,"item_added_to_cart",user_id=user_id,quantity=quantity,product_name=pname))
    keyboard = [
        [InlineKeyboardButton(await _(context,"add_more_products_button",user_id=user_id), callback_data="order_flow_browse_return")],
        [InlineKeyboardButton(await _(context,"view_cart_button",user_id=user_id), callback_data="order_flow_view_cart_state")],
        [InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=user_id), callback_data="main_menu_direct_cb")]
    ]
    await update.message.reply_text(await _(context,"what_next_prompt",user_id=user_id),reply_markup=InlineKeyboardMarkup(keyboard))
    return ORDER_FLOW_BROWSING_PRODUCTS # Go back to product list or a state that handles these buttons

async def order_flow_view_cart_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    return await order_flow_display_cart(update, context, user_id, edit_message=True)

async def order_flow_view_cart_direct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: # Direct access from main menu
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    context.user_data.setdefault('cart', []) # Ensure cart exists
    # This should enter the order conversation if it's not already in one.
    # For now, let's assume it displays cart and buttons that would lead into the conversation.
    # This is tricky. A better way is for this button to be an entry point to the order_conv at VIEW_CART_STATE.
    # For now, let's just display.
    await order_flow_display_cart(update, context, user_id, edit_message=True)
    return ORDER_FLOW_VIEWING_CART # Return a state, assuming this is now part of the order conv

async def order_flow_display_cart(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, edit_message: bool):
    cart = context.user_data.get('cart', [])
    target_message = update.callback_query.message if edit_message and update.callback_query else update.message
    
    text, keyboard_buttons = "", []
    if not cart:
        text = await _(context,"cart_empty",user_id=user_id)
        keyboard_buttons.append([InlineKeyboardButton(await _(context,"browse_products_button",user_id=user_id),callback_data="order_flow_browse_return")])
    else:
        text = await _(context,"your_cart_title",user_id=user_id)+"\n"; total_price=0
        for i,item in enumerate(cart):
            item_total=item['price']*item['quantity']; total_price+=item_total
            text+=f"{i+1}. {item['name']} - {item['quantity']} kg x {item['price']:.2f} EUR = {item_total:.2f} EUR\n"
            keyboard_buttons.append([InlineKeyboardButton(await _(context,"remove_item_button",user_id=user_id,item_index=i+1),callback_data=f"order_flow_remove_{i}")])
        text+="\n"+await _(context,"cart_total",user_id=user_id,total_price=total_price)
        keyboard_buttons.append([InlineKeyboardButton(await _(context,"checkout_button",user_id=user_id),callback_data="order_flow_checkout")])
        keyboard_buttons.append([InlineKeyboardButton(await _(context,"add_more_products_button",user_id=user_id),callback_data="order_flow_browse_return")])

    keyboard_buttons.append([InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=user_id),callback_data="main_menu_direct_cb")])
    
    if edit_message: await target_message.edit_text(text=text,reply_markup=InlineKeyboardMarkup(keyboard_buttons))
    else: await target_message.reply_text(text=text,reply_markup=InlineKeyboardMarkup(keyboard_buttons))
    return ORDER_FLOW_VIEWING_CART


async def order_flow_remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query=update.callback_query; await query.answer(); user_id=query.from_user.id
    try: idx=int(query.data.split('_')[-1])
    except: await query.message.reply_text(await _(context,"generic_error_message",user_id=user_id)); return ORDER_FLOW_VIEWING_CART
    cart=context.user_data.get('cart',[])
    if 0<=idx<len(cart): removed=cart.pop(idx); await query.message.reply_text(await _(context,"item_removed_from_cart",user_id=user_id,item_name=removed['name']))
    else: await query.message.reply_text(await _(context,"invalid_item_to_remove",user_id=user_id))
    return await order_flow_display_cart(update,context,user_id,edit_message=True)


async def order_flow_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query=update.callback_query; await query.answer(); user_id=query.from_user.id
    cart=context.user_data.get('cart',[])
    if not cart: await query.edit_message_text(await _(context,"cart_empty",user_id=user_id)); return ORDER_FLOW_VIEWING_CART
    
    user=query.effective_user; uname=user.full_name or ""; total=sum(i['price']*i['quantity'] for i in cart)
    oid=save_order_to_db(user_id,uname,cart,total)
    if oid:
        await query.edit_message_text(await _(context,"order_placed_success",user_id=user_id,order_id=oid,total_price=total))
        admin_msg=f"🔔 New Order #{oid} from {uname} (@{user.username or ''}, ID:{user_id})\nTotal:{total:.2f} EUR\nItems:\n"
        for item in cart: admin_msg+=f"- {item['name']}: {item['quantity']} kg\n"
        if ADMIN_IDS: 
            for admin_id in ADMIN_IDS: 
                try: await context.bot.send_message(chat_id=admin_id,text=admin_msg)
                except Exception as e: logger.error(f"Notify admin {admin_id} error: {e}")
        lang=context.user_data.get('language_code'); context.user_data.clear()
        if lang: context.user_data['language_code']=lang
        # Create a new message for the main menu as the current one is edited.
        temp_update = Update(update_id=0); temp_update.effective_user = user; temp_update.message = query.message
        await display_main_menu(temp_update, context, edit_message=False) # Send new message
    else:
        await query.edit_message_text(await _(context,"order_placed_error",user_id=user_id))
        # After error, offer to go back to cart or main menu
        kb = [[InlineKeyboardButton(await _(context,"view_cart_button",user_id=user_id),callback_data="order_flow_view_cart_state")],
              [InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=user_id),callback_data="main_menu_direct_cb")]]
        await query.message.reply_text("What next?", reply_markup=InlineKeyboardMarkup(kb)) # New message for options
        return ORDER_FLOW_VIEWING_CART # Allow user to see cart again

    return ConversationHandler.END


async def my_orders_direct(update: Update, context: ContextTypes.DEFAULT_TYPE): # Not part of a conversation
    query=update.callback_query; await query.answer(); user_id=query.from_user.id
    orders=get_user_orders_from_db(user_id)
    text = await _(context,"my_orders_title",user_id=user_id)+"\n\n" if orders else await _(context,"no_orders_yet",user_id=user_id)
    if orders:
        for oid,date,total,status,items in orders: text+=await _(context,"order_details_format",user_id=user_id,order_id=oid,date=date,status=status.capitalize(),total=total,items=items)
    kb=[[InlineKeyboardButton(await _(context,"back_to_main_menu_button",user_id=user_id),callback_data="main_menu_direct_cb")]]
    await query.edit_message_text(text=text,reply_markup=InlineKeyboardMarkup(kb))


# --- Admin Panel ---
async def display_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    user_id = update.effective_user.id
    if not (ADMIN_IDS and user_id in ADMIN_IDS): 
        await (update.callback_query.message if edit_message and update.callback_query else update.message).reply_text(await _(context,"admin_unauthorized",user_id=user_id))
        return ConversationHandler.END # End if not admin
    context.chat_data['user_id_for_translation'] = user_id
    kb = [
        [InlineKeyboardButton(await _(context,"admin_add_product_button",user_id=user_id),callback_data="admin_add_prod_entry")],
        [InlineKeyboardButton(await _(context,"admin_manage_products_button",user_id=user_id),callback_data="admin_manage_prod_list_entry")],
        [InlineKeyboardButton(await _(context,"admin_view_orders_button",user_id=user_id),callback_data="admin_view_orders_direct")],
        [InlineKeyboardButton(await _(context,"admin_shopping_list_button",user_id=user_id),callback_data="admin_shop_list_direct")],
        [InlineKeyboardButton(await _(context,"admin_exit_button",user_id=user_id),callback_data="main_menu_direct_cb")] # Exit admin goes to user main menu
    ]
    title = await _(context,"admin_panel_title",user_id=user_id)
    target = update.callback_query.message if edit_message and update.callback_query else update.message
    try:
        if edit_message: await target.edit_text(title,reply_markup=InlineKeyboardMarkup(kb))
        else: await target.reply_text(title,reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: logger.warning(f"Display admin panel error: {e}"); await update.message.reply_text(title,reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MAIN_PANEL_STATE

async def admin_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await display_admin_panel(update,context)
async def admin_panel_return_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query: await update.callback_query.answer()
    return await display_admin_panel(update,context,edit_message=True)

# Admin Add Product Flow
async def admin_add_prod_entry(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    q=update.callback_query; await q.answer(); uid=q.from_user.id
    await q.edit_message_text(await _(context,"admin_enter_product_name",user_id=uid))
    return ADMIN_ADD_PROD_NAME
async def admin_add_prod_name_state(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    uid=update.effective_user.id; pname=update.message.text; context.user_data['new_pname']=pname
    await update.message.reply_text(await _(context,"admin_enter_product_price",user_id=uid,product_name=pname))
    return ADMIN_ADD_PROD_PRICE
async def admin_add_prod_price_state(update:Update,context:ContextTypes.DEFAULT_TYPE)->int:
    uid=update.effective_user.id; name=context.user_data.get('new_pname')
    try: price=float(update.message.text); assert price>0
    except: await update.message.reply_text(await _(context,"admin_invalid_price",user_id=uid)); return ADMIN_ADD_PROD_PRICE
    if not name: await update.message.reply_text(await _(context,"generic_error_message",user_id=uid)); return await admin_panel_return_cb(update,context)
    
    msg_key = "admin_product_added" if add_product_to_db(name,price) else "admin_product_add_failed"
    await update.message.reply_text(await _(context,msg_key,user_id=uid,product_name=name,price=price if msg_key=="admin_product_added" else None))
    if 'new_pname' in context.user_data: del context.user_data['new_pname']
    # After adding, display admin panel again (as a new message to avoid complexity with current message being from user)
    temp_update = Update(update_id=0); temp_update.effective_user = update.effective_user; temp_update.message = update.message
    await display_admin_panel(temp_update, context, edit_message=False)
    return ConversationHandler.END

# Admin Manage Products Flow (Re-integrated)
async def admin_manage_prod_list_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    products = get_products_from_db(available_only=False)
    keyboard = []
    if not products:
        text = await _(context, "admin_no_products_to_manage", user_id=user_id)
        keyboard.append([InlineKeyboardButton(await _(context, "admin_back_to_admin_panel_button", user_id=user_id), callback_data="admin_panel_return_cb_data")])
    else:
        text = await _(context, "admin_select_product_to_manage", user_id=user_id)
        for pid, name, price, avail in products:
            status = await _(context, "admin_status_available" if avail else "admin_status_unavailable", user_id=user_id)
            keyboard.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR ({status})", callback_data=f"admin_manage_select_{pid}")])
        keyboard.append([InlineKeyboardButton(await _(context, "admin_back_to_admin_panel_button", user_id=user_id), callback_data="admin_panel_return_cb_data")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_MANAGE_PROD_LIST

async def admin_manage_prod_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    try: product_id = int(query.data.split('_')[-1])
    except: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id)); return ADMIN_MANAGE_PROD_LIST
    
    product = get_product_by_id(product_id)
    if not product: await query.edit_message_text(await _(context,"product_not_found",user_id=user_id)); return ADMIN_MANAGE_PROD_LIST
    
    context.user_data['editing_pid'] = product_id # Store ID of product being edited
    pname, pprice, pavail = product[1], product[2], product[3]
    avail_text = await _(context, "admin_set_unavailable_button" if pavail else "admin_set_available_button", user_id=user_id)
    keyboard = [
        [InlineKeyboardButton(await _(context,"admin_change_price_button",user_id=user_id,price=pprice), callback_data=f"admin_manage_edit_price_entry")],
        [InlineKeyboardButton(avail_text, callback_data=f"admin_manage_toggle_avail_{1-pavail}")], # Pass new state
        [InlineKeyboardButton(await _(context,"admin_delete_product_button",user_id=user_id), callback_data=f"admin_manage_delete_confirm")],
        [InlineKeyboardButton(await _(context,"admin_back_to_product_list_button",user_id=user_id), callback_data="admin_manage_prod_list_cb_refresh")]
    ]
    await query.edit_message_text(await _(context,"admin_managing_product",user_id=user_id,product_name=pname), reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_MANAGE_PROD_OPTIONS

async def admin_manage_edit_price_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id)); return ADMIN_MANAGE_PROD_LIST
    product = get_product_by_id(editing_pid)
    if not product: await query.edit_message_text(await _(context,"product_not_found",user_id=user_id)); return ADMIN_MANAGE_PROD_LIST
    await query.edit_message_text(await _(context,"admin_enter_new_price",user_id=user_id,product_name=product[1],current_price=product[2]))
    return ADMIN_MANAGE_PROD_EDIT_PRICE

async def admin_manage_edit_price_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; new_price_str = update.message.text
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: await update.message.reply_text(await _(context,"generic_error_message",user_id=user_id)); return await admin_panel_return_cb(update,context)
    try: new_price = float(new_price_str); assert new_price > 0
    except: await update.message.reply_text(await _(context,"admin_invalid_price",user_id=user_id)); return ADMIN_MANAGE_PROD_EDIT_PRICE
    
    msg_key = "admin_price_updated" if update_product_in_db(editing_pid,price=new_price) else "admin_price_update_failed"
    await update.message.reply_text(await _(context,msg_key,user_id=user_id,product_id=editing_pid))
    # After price edit, go back to product options for the same product
    # Need to reconstruct the query-like update for admin_manage_prod_selected
    # This is complex. A simpler way is to go back to the product list or admin panel.
    # For now, back to product list.
    # Create a pseudo update for admin_manage_prod_list_entry
    temp_update = Update(update_id=0); temp_update.effective_user = update.effective_user
    # admin_manage_prod_list_entry expects a callback_query
    class MockQuery: pass
    temp_update.callback_query = MockQuery()
    temp_update.callback_query.from_user = update.effective_user
    temp_update.callback_query.message = update.message # The message to potentially edit
    async def mock_answer(): pass
    temp_update.callback_query.answer = mock_answer

    return await admin_manage_prod_list_entry(temp_update, context) # Re-list products

async def admin_manage_toggle_avail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id)); return ADMIN_MANAGE_PROD_LIST
    try: new_avail_state = int(query.data.split('_')[-1])
    except: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id)); return ADMIN_MANAGE_PROD_OPTIONS
    
    msg_key = "admin_product_set_status" if update_product_in_db(editing_pid,is_available=new_avail_state) else "admin_status_update_failed"
    status_text = await _(context, "admin_status_available_text" if new_avail_state else "admin_status_unavailable_text", user_id=user_id)
    await query.edit_message_text(await _(context,msg_key,user_id=user_id,product_id=editing_pid,status_text=status_text))
    # Re-show options for this product
    query.data = f"admin_manage_select_{editing_pid}" # Modify query data to re-select same product
    return await admin_manage_prod_selected(update, context)

async def admin_manage_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id)); return ADMIN_MANAGE_PROD_LIST
    product = get_product_by_id(editing_pid)
    if not product: await query.edit_message_text(await _(context,"product_not_found",user_id=user_id)); return ADMIN_MANAGE_PROD_LIST
    
    kb = [
        [InlineKeyboardButton(await _(context,"admin_confirm_delete_yes_button",user_id=user_id,product_name=product[1]), callback_data="admin_manage_delete_do")],
        [InlineKeyboardButton(await _(context,"admin_confirm_delete_no_button",user_id=user_id), callback_data=f"admin_manage_select_{editing_pid}")] # Back to options
    ]
    await query.edit_message_text(await _(context,"admin_confirm_delete_prompt",user_id=user_id,product_name=product[1]),reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MANAGE_PROD_DELETE_CONFIRM

async def admin_manage_delete_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    editing_pid = context.user_data.get('editing_pid')
    if not editing_pid: await query.edit_message_text(await _(context,"generic_error_message",user_id=user_id)); return ADMIN_MANAGE_PROD_LIST

    msg_key = "admin_product_deleted" if delete_product_from_db(editing_pid) else "admin_product_delete_failed"
    await query.edit_message_text(await _(context,msg_key,user_id=user_id,product_id=editing_pid))
    if 'editing_pid' in context.user_data: del context.user_data['editing_pid']
    # After delete, go back to product list
    return await admin_manage_prod_list_entry(update, context)


# Direct Admin Actions (not part of multi-step conversations)
async def admin_view_orders_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer(); user_id=query.from_user.id
    orders=get_all_orders_from_db()
    text = await _(context,"admin_all_orders_title",user_id=user_id) if orders else await _(context,"admin_no_orders_found",user_id=user_id)
    if orders:
        for oid,cuid,uname,date,total,status,items in orders: text+=await _(context,"admin_order_details_format",user_id=user_id,order_id=oid,user_name=uname,customer_id=cuid,date=date,total=total,status=status.capitalize(),items=items)
    if len(text)>4000: text=text[:4000]+"\n...(truncated)"
    kb=[[InlineKeyboardButton(await _(context,"admin_back_to_admin_panel_button",user_id=user_id),callback_data="admin_panel_return_cb_data")]]
    await query.edit_message_text(text=text,reply_markup=InlineKeyboardMarkup(kb))

async def admin_shop_list_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer(); user_id=query.from_user.id
    slist=get_shopping_list_from_db()
    text=await _(context,"admin_shopping_list_title",user_id=user_id) if slist else await _(context,"admin_shopping_list_empty",user_id=user_id)
    if slist:
        for name,qty in slist: text+=await _(context,"admin_shopping_list_item_format",user_id=user_id,name=name,total_quantity=qty)
    kb=[[InlineKeyboardButton(await _(context,"admin_back_to_admin_panel_button",user_id=user_id),callback_data="admin_panel_return_cb_data")]]
    await query.edit_message_text(text=text,reply_markup=InlineKeyboardMarkup(kb))

async def general_cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if update.callback_query: await update.callback_query.answer()
    await (update.callback_query.message if update.callback_query else update.message).reply_text(
        await _(context, "action_cancelled", user_id=user_id))
    
    # Preserve language, clear cart if it exists
    lang = context.user_data.get('language_code')
    context.user_data.pop('cart', None) 
    context.user_data.pop('editing_pid', None)
    context.user_data.pop('new_pname', None)
    # Add other conversation specific keys to pop if needed

    if lang: context.user_data['language_code'] = lang

    if ADMIN_IDS and user_id in ADMIN_IDS:
        return await display_admin_panel(update, context, edit_message=bool(update.callback_query))
    else:
        return await display_main_menu(update, context, edit_message=bool(update.callback_query))
    # This should return ConversationHandler.END, but since it's a general cancel,
    # the ConversationHandler using it as a fallback will handle the END.


def main() -> None:
    global ADMIN_IDS 
    if not TELEGRAM_TOKEN or not ADMIN_TELEGRAM_ID: logger.critical("Tokens missing!"); return
    try: ADMIN_IDS = [int(aid.strip()) for aid in ADMIN_TELEGRAM_ID.split(',')]
    except: logger.critical("Admin IDs invalid!"); return
    load_translations(); init_db()
    if not translations.get("en") or not translations.get("lt"): logger.critical("Translations missing!"); return
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # General "Back to Main Menu" from anywhere (ends current conversation)
    back_to_main_menu_conv_ender = CallbackQueryHandler(back_to_main_menu_handler, pattern="^main_menu_direct_cb$")
    general_cancel_conv_ender = CommandHandler("cancel", general_cancel_handler)


    # --- Conversation Handlers ---
    lang_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_language_entry, pattern="^select_language_entry$")],
        states={SELECT_LANGUAGE_STATE: [CallbackQueryHandler(language_selected_state, pattern="^lang_(en|lt)$")]},
        fallbacks=[back_to_main_menu_conv_ender, general_cancel_conv_ender]
    )

    order_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(order_flow_browse_entry, pattern="^order_flow_browse_entry$"),
                      CallbackQueryHandler(order_flow_view_cart_direct, pattern="^order_flow_view_cart_direct$")], # Direct entry to cart view
        states={
            ORDER_FLOW_BROWSING_PRODUCTS: [
                CallbackQueryHandler(order_flow_product_selected, pattern="^order_flow_select_prod_\d+$"),
                CallbackQueryHandler(order_flow_view_cart_state, pattern="^order_flow_view_cart_state$"),
                CallbackQueryHandler(lambda u,c: order_flow_list_products(u,c,u.callback_query.from_user.id), pattern="^order_flow_browse_return$"),
            ],
            ORDER_FLOW_SELECTING_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_flow_quantity_typed)],
            ORDER_FLOW_VIEWING_CART: [
                CallbackQueryHandler(order_flow_remove_item, pattern="^order_flow_remove_\d+$"),
                CallbackQueryHandler(order_flow_checkout, pattern="^order_flow_checkout$"),
                CallbackQueryHandler(lambda u,c: order_flow_list_products(u,c,u.callback_query.from_user.id), pattern="^order_flow_browse_return$"),
            ]
        },
        fallbacks=[back_to_main_menu_conv_ender, general_cancel_conv_ender, CommandHandler("start", start_command)]
    )

    admin_add_prod_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_prod_entry, pattern="^admin_add_prod_entry$")],
        states={
            ADMIN_ADD_PROD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_prod_name_state)],
            ADMIN_ADD_PROD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_prod_price_state)],
        },
        fallbacks=[CallbackQueryHandler(admin_panel_return_cb, pattern="^admin_panel_return_cb_data$"), general_cancel_conv_ender]
    )

    admin_manage_prod_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_manage_prod_list_entry, pattern="^admin_manage_prod_list_entry$")],
        states={
            ADMIN_MANAGE_PROD_LIST: [CallbackQueryHandler(admin_manage_prod_selected, pattern="^admin_manage_select_\d+$")],
            ADMIN_MANAGE_PROD_OPTIONS: [
                CallbackQueryHandler(admin_manage_edit_price_entry, pattern="^admin_manage_edit_price_entry$"),
                CallbackQueryHandler(admin_manage_toggle_avail, pattern="^admin_manage_toggle_avail_(0|1)$"),
                CallbackQueryHandler(admin_manage_delete_confirm, pattern="^admin_manage_delete_confirm$"),
                CallbackQueryHandler(admin_manage_prod_list_entry, pattern="^admin_manage_prod_list_cb_refresh$") # Refresh list
            ],
            ADMIN_MANAGE_PROD_EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_manage_edit_price_state)],
            ADMIN_MANAGE_PROD_DELETE_CONFIRM: [
                CallbackQueryHandler(admin_manage_delete_do, pattern="^admin_manage_delete_do$"),
                CallbackQueryHandler(admin_manage_prod_selected, pattern="^admin_manage_select_\d+$") # If they cancel delete, go back to options (data needs to be correct)
            ]
        },
        fallbacks=[CallbackQueryHandler(admin_panel_return_cb, pattern="^admin_panel_return_cb_data$"), general_cancel_conv_ender]
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(lang_conv)
    application.add_handler(order_conv)
    
    application.add_handler(CallbackQueryHandler(my_orders_direct, pattern="^my_orders_direct$"))
    
    application.add_handler(CommandHandler("admin", admin_command_entry))
    application.add_handler(admin_add_prod_conv)
    application.add_handler(admin_manage_prod_conv)
    application.add_handler(CallbackQueryHandler(admin_view_orders_direct, pattern="^admin_view_orders_direct$"))
    application.add_handler(CallbackQueryHandler(admin_shop_list_direct, pattern="^admin_shop_list_direct$"))
    # General back to admin panel if not caught by a conversation's fallback
    application.add_handler(CallbackQueryHandler(admin_panel_return_cb, pattern="^admin_panel_return_cb_data$"))


    logger.info("Bot starting...")
    application.run_polling()

if __name__ == "__main__": main()
