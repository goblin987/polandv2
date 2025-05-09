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
    
    lang_translations = translations.get(lang_code, translations.get(DEFAULT_LANGUAGE, translations.get("en", {})))
    text_to_return = lang_translations.get(key)
    
    if text_to_return is None: 
        if lang_code != DEFAULT_LANGUAGE:
            text_to_return = translations.get(DEFAULT_LANGUAGE, {}).get(key)
    if text_to_return is None:
        if lang_code != "en" and DEFAULT_LANGUAGE != "en": 
             text_to_return = translations.get("en", {}).get(key)
    if text_to_return is None: 
        logger.warning(f"Translation key '{key}' not found in '{lang_code}', default, or 'en'. Using key itself.")
        text_to_return = kwargs.get("default", key) # Use provided default if key is missing

    try:
        return text_to_return.format(**kwargs) if isinstance(text_to_return, str) else str(text_to_return)
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

def get_product_by_id(product_id):
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); product = None
    try: cursor.execute("SELECT id, name, price_per_kg, is_available FROM products WHERE id = ?", (product_id,)); product = cursor.fetchone()
    except sqlite3.Error as e: logger.error(f"DB error getting product by ID {product_id}: {e}")
    finally: conn.close()
    return product

def update_product_in_db(product_id, name=None, price=None, is_available=None):
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); success = False
    try:
        fields, params = [], []
        if name is not None: fields.append("name = ?"); params.append(name)
        if price is not None: fields.append("price_per_kg = ?"); params.append(price)
        if is_available is not None: fields.append("is_available = ?"); params.append(is_available)
        if not fields: conn.close(); return False
        params.append(product_id); query = f"UPDATE products SET {', '.join(fields)} WHERE id = ?"
        cursor.execute(query, tuple(params)); conn.commit(); success = True
    except sqlite3.Error as e: logger.error(f"Error updating product {product_id}: {e}")
    finally: conn.close()
    return success

def delete_product_from_db(product_id):
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); success = False
    try: cursor.execute("DELETE FROM products WHERE id = ?", (product_id,)); conn.commit(); success = True
    except sqlite3.Error as e: logger.error(f"Error deleting product {product_id}: {e}")
    finally: conn.close()
    return success

def save_order_to_db(user_id, user_name, cart, total_price):
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); order_id = None
    order_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute("INSERT INTO orders (user_id, user_name, order_date, total_price) VALUES (?, ?, ?, ?)", (user_id, user_name, order_date, total_price))
        order_id = cursor.lastrowid
        for item in cart: cursor.execute("INSERT INTO order_items (order_id, product_id, quantity_kg, price_at_order) VALUES (?, ?, ?, ?)", (order_id, item['id'], item['quantity'], item['price']))
        conn.commit()
    except sqlite3.Error as e: logger.error(f"Error saving order for user {user_id}: {e}"); order_id = None
    finally: conn.close()
    return order_id

def get_user_orders_from_db(user_id):
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); orders = []
    try:
        cursor.execute("SELECT o.id, o.order_date, o.total_price, o.status, group_concat(p.name || ' (' || oi.quantity_kg || 'kg)', ', ') FROM orders o JOIN order_items oi ON o.id = oi.order_id JOIN products p ON oi.product_id = p.id WHERE o.user_id = ? GROUP BY o.id ORDER BY o.order_date DESC", (user_id,))
        orders = cursor.fetchall()
    except sqlite3.Error as e: logger.error(f"DB error getting orders for user {user_id}: {e}")
    finally: conn.close()
    return orders

def get_all_orders_from_db():
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); orders = []
    try:
        cursor.execute("SELECT o.id, o.user_id, o.user_name, o.order_date, o.total_price, o.status, GROUP_CONCAT(p.name || ' (' || oi.quantity_kg || 'kg @ ' || oi.price_at_order || ' EUR)', CHAR(10)) as items_details FROM orders o JOIN order_items oi ON o.id = oi.order_id JOIN products p ON oi.product_id = p.id GROUP BY o.id ORDER BY o.order_date DESC")
        orders = cursor.fetchall()
    except sqlite3.Error as e: logger.error(f"DB error getting all orders: {e}")
    finally: conn.close()
    return orders
    
def get_shopping_list_from_db():
    conn = sqlite3.connect(DB_NAME); cursor = conn.cursor(); shopping_list = []
    try:
        cursor.execute("SELECT p.name, SUM(oi.quantity_kg) as total_quantity FROM order_items oi JOIN products p ON oi.product_id = p.id JOIN orders o ON oi.order_id = o.id WHERE o.status IN ('pending', 'confirmed') GROUP BY p.name ORDER BY p.name")
        shopping_list = cursor.fetchall()
    except sqlite3.Error as e: logger.error(f"DB error getting shopping list: {e}")
    finally: conn.close()
    return shopping_list

# Conversation States (ensure these are distinct if used across different handlers or make them specific)
(MAIN_MENU, BROWSE_PRODUCTS_STATE, SELECTING_PRODUCT_QTY, VIEW_CART_STATE, # User order flow
 ADMIN_PANEL_STATE, ADMIN_ADD_PRODUCT_NAME, ADMIN_ADD_PRODUCT_PRICE, # Admin add product
 ADMIN_MANAGE_PRODUCTS_LIST, ADMIN_MANAGE_PRODUCT_OPTIONS, ADMIN_EDIT_PRODUCT_PRICE, # Admin manage product
 SELECT_LANGUAGE_STATE # Language selection
) = range(11)


async def display_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    user = update.effective_user
    user_id = user.id # Get user_id for translation
    
    # Ensure language is set in context if not already
    if 'language_code' not in context.user_data:
        context.user_data['language_code'] = await get_user_language(context, user_id)

    keyboard = [
        [InlineKeyboardButton(await _(context, "browse_products_button", user_id=user_id), callback_data="browse_products_entry")],
        [InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="view_cart_direct")], # Direct cart view
        [InlineKeyboardButton(await _(context, "my_orders_button", user_id=user_id), callback_data="my_orders_direct")], # Direct my orders
        [InlineKeyboardButton(await _(context, "set_language_button", user_id=user_id), callback_data="set_language_entry")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = await _(context, "welcome_message", user_id=user_id, user_mention=user.mention_html())
    
    target_message = update.callback_query.message if edit_message and update.callback_query else update.message
    
    if edit_message and target_message:
        try:
            await target_message.edit_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')
        except Exception as e: # Fallback if edit fails
            logger.warning(f"Failed to edit message for main menu, sending new: {e}")
            await update.message.reply_html(welcome_text, reply_markup=reply_markup)
    else:
        await update.message.reply_html(welcome_text, reply_markup=reply_markup)
    return MAIN_MENU # Return a general main menu state or ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: # Changed to return int for ConvHandler
    user = update.effective_user
    first_name_str = user.first_name or ""
    username_str = user.username or ""
    await ensure_user_exists(user.id, first_name_str, username_str, context) 
    
    # Clear previous conversation states if any, but preserve essential user_data like language
    current_lang_code = context.user_data.get('language_code')
    # context.user_data.clear() # Avoid clearing if it disrupts other flows.
    # If specific conv data needs clearing, do it in that conv's end/cancel.
    if current_lang_code:
        context.user_data['language_code'] = current_lang_code
    else:
        context.user_data['language_code'] = await get_user_language(context, user.id)

    return await display_main_menu(update, context)


async def set_language_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("English 🇬🇧", callback_data="lang_en")],
        [InlineKeyboardButton("Lietuvių 🇱🇹", callback_data="lang_lt")],
        [InlineKeyboardButton(await _(context, "back_button", user_id=user_id, default="⬅️ Back"), callback_data="back_to_main_menu_direct")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    prompt_text = await _(context, "choose_language", user_id=user_id)
    await query.edit_message_text(text=prompt_text, reply_markup=reply_markup)
    return SELECT_LANGUAGE_STATE

async def language_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang_code = query.data.split('_')[1]  
    user_id = query.from_user.id
    context.user_data['language_code'] = lang_code
    await set_user_language_db(user_id, lang_code)
    lang_name = "English" if lang_code == "en" else "Lietuvių"
    await query.edit_message_text(await _(context, "language_set_to", user_id=user_id, language_name=lang_name))
    # After setting language, show main menu again
    await display_main_menu(update, context, edit_message=True)
    return ConversationHandler.END # End the language selection conversation

async def back_to_main_menu_direct_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles direct "back to main menu" button clicks, ensuring conv is ended."""
    if update.callback_query:
        await update.callback_query.answer()
    await display_main_menu(update, context, edit_message=bool(update.callback_query))
    return ConversationHandler.END # Crucial to end any active conversation


# --- User Order Flow ---
async def browse_products_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # This is the entry point for the order conversation
    query = update.callback_query
    await query.answer()
    # Call the actual product browsing logic
    return await browse_products_action(update, context, query.from_user.id)

async def browse_products_action(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Displays products. Can be called from entry or 'add more'."""
    products = get_products_from_db(available_only=True)
    query = update.callback_query # Assume this is always called from a query context in the conv

    if not products:
        await query.edit_message_text(text=await _(context, "no_products_available", user_id=user_id)) 
        # Offer to go back to main menu
        keyboard = [[InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="conv_end_to_main_menu")]]
        await query.message.reply_text(text=await _(context, "no_products_available", user_id=user_id), reply_markup=InlineKeyboardMarkup(keyboard))
        return BROWSE_PRODUCTS_STATE # Or a specific state to handle this

    keyboard = []
    for prod_id, name, price, is_available_status in products: 
        keyboard.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR/kg", callback_data=f"prod_{prod_id}")])
    
    keyboard.append([InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="view_cart_state")])
    keyboard.append([InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="conv_end_to_main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text=await _(context, "products_title", user_id=user_id), reply_markup=reply_markup)
    return BROWSE_PRODUCTS_STATE


async def product_selected_for_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    product_id_str = query.data.split('_')[1]
    try: product_id = int(product_id_str)
    except ValueError:
        logger.error(f"Invalid product_id in callback_data: {query.data}")
        await query.edit_message_text(text=await _(context, "generic_error_message", user_id=user_id, default="An error occurred.")) 
        return BROWSE_PRODUCTS_STATE # Back to browsing

    product = get_product_by_id(product_id)
    if not product:
        await query.edit_message_text(text=await _(context, "product_not_found", user_id=user_id, default="Product not found.")) 
        return BROWSE_PRODUCTS_STATE # Back to browsing

    context.user_data['current_product_id'] = product_id
    context.user_data['current_product_name'] = product[1] 
    context.user_data['current_product_price'] = product[2]
    prompt_text = await _(context, "product_selected_prompt", user_id=user_id, product_name=product[1])
    await query.edit_message_text(text=prompt_text)
    return SELECTING_PRODUCT_QTY


async def quantity_typed_for_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text
    user_id = update.effective_user.id
    try:
        quantity = float(user_input)
        if quantity <= 0: raise ValueError("Quantity must be positive.")
    except ValueError:
        await update.message.reply_text(await _(context, "invalid_quantity_prompt", user_id=user_id))
        return SELECTING_PRODUCT_QTY # Stay in state

    product_id = context.user_data.get('current_product_id')
    # ... (rest of quantity_typed logic from before) ...
    product_name = context.user_data.get('current_product_name')
    product_price = context.user_data.get('current_product_price')

    if not all([product_id, product_name, product_price is not None]):
        logger.error(f"Missing product data in quantity_typed for user {user_id}")
        await update.message.reply_text(await _(context, "generic_error_message", user_id=user_id, default="Error processing. Please start over."))
        # This should end the conversation and guide user back
        await display_main_menu(update, context)
        return ConversationHandler.END

    if 'cart' not in context.user_data: context.user_data['cart'] = []
    # Add/update item in cart
    found = False
    for item in context.user_data['cart']:
        if item['id'] == product_id: item['quantity'] += quantity; found = True; break
    if not found: context.user_data['cart'].append({'id': product_id, 'name': product_name, 'price': product_price, 'quantity': quantity})
    
    await update.message.reply_text(await _(context, "item_added_to_cart", user_id=user_id, quantity=quantity, product_name=product_name))
    
    # After adding, show options: Add More, View Cart, Main Menu
    keyboard = [
        [InlineKeyboardButton(await _(context, "add_more_products_button", user_id=user_id), callback_data="browse_products_state_return")], # Go back to product browsing state
        [InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="view_cart_state")],
        [InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="conv_end_to_main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(await _(context, "what_next_prompt", user_id=user_id), reply_markup=reply_markup)
    return BROWSE_PRODUCTS_STATE # Return to a state that can handle these new buttons

async def view_cart_state_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id # Ensure user_id
    if query: await query.answer()
    else: # If called not from query (e.g. direct command, though not set up here)
        # This path needs an update object that view_cart_action can use (or view_cart_action needs to handle both)
        logger.warning("view_cart_state_handler called without query, not fully supported yet for message editing.")

    return await view_cart_action(update, context, user_id, edit_message=bool(query))


async def view_cart_action(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, edit_message: bool) -> int:
    cart = context.user_data.get('cart', [])
    target_message = update.callback_query.message if edit_message and update.callback_query else update.message

    if not cart:
        message = await _(context, "cart_empty", user_id=user_id)
        keyboard = [
            [InlineKeyboardButton(await _(context, "browse_products_button", user_id=user_id), callback_data="browse_products_state_return")],
            [InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="conv_end_to_main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if edit_message: await target_message.edit_text(text=message, reply_markup=reply_markup)
        else: await target_message.reply_text(text=message, reply_markup=reply_markup)
        return VIEW_CART_STATE

    cart_summary = await _(context, "your_cart_title", user_id=user_id) + "\n"
    # ... (cart summary calculation from before) ...
    total_price = 0
    remove_buttons_rows = []
    for i, item in enumerate(cart):
        item_total = item['price'] * item['quantity']
        cart_summary += f"{i+1}. {item['name']} - {item['quantity']} kg x {item['price']:.2f} EUR = {item_total:.2f} EUR\n"
        total_price += item_total
        # Prepare remove buttons in rows of 1 for simplicity, or more complex layout
        remove_buttons_rows.append([InlineKeyboardButton(await _(context, "remove_item_button", user_id=user_id, item_index=i+1), callback_data=f"remove_item_{i}")])
    cart_summary += "\n" + await _(context, "cart_total", user_id=user_id, total_price=total_price)

    keyboard = remove_buttons_rows # Add all remove buttons
    keyboard.extend([
        [InlineKeyboardButton(await _(context, "checkout_button", user_id=user_id), callback_data="checkout_order")],
        [InlineKeyboardButton(await _(context, "add_more_products_button", user_id=user_id), callback_data="browse_products_state_return")],
        [InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="conv_end_to_main_menu")]
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if edit_message: await target_message.edit_text(text=cart_summary, reply_markup=reply_markup)
    else: await target_message.reply_text(text=cart_summary, reply_markup=reply_markup)
    return VIEW_CART_STATE


async def remove_item_from_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    # ... (remove item logic from before) ...
    item_index_to_remove_str = query.data.split('_')[2] # remove_item_INDEX
    try: item_index_to_remove = int(item_index_to_remove_str)
    except ValueError: # ... error handling ...
        await query.message.reply_text(await _(context, "generic_error_message", user_id=user_id, default="Error."))
        return await view_cart_action(update, context, user_id, edit_message=True)

    cart = context.user_data.get('cart', [])
    if 0 <= item_index_to_remove < len(cart):
        removed_item = cart.pop(item_index_to_remove)
        await query.message.reply_text(await _(context, "item_removed_from_cart", user_id=user_id, item_name=removed_item['name']))
    else: await query.message.reply_text(await _(context, "invalid_item_to_remove", user_id=user_id))
    
    return await view_cart_action(update, context, user_id, edit_message=True) # Refresh cart view


async def checkout_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    cart = context.user_data.get('cart', [])
    # ... (checkout logic from before, then call display_main_menu) ...
    if not cart: # ... handle empty cart ...
        await query.edit_message_text(await _(context, "cart_empty", user_id=user_id))
        return VIEW_CART_STATE # Or back to browsing

    user = query.effective_user
    user_full_name_str = user.full_name or "N/A"; user_username_str = user.username or "N/A"
    total_price = sum(item['price'] * item['quantity'] for item in cart)
    order_id = save_order_to_db(user.id, user_full_name_str, cart, total_price)

    if order_id:
        await query.edit_message_text(await _(context, "order_placed_success", user_id=user_id, order_id=order_id, total_price=total_price))
        # Admin notification
        admin_message = f"🔔 New Order #{order_id} from {user_full_name_str} (@{user_username_str}, ID: {user.id})\nTotal: {total_price:.2f} EUR\nItems:\n"
        for item in cart: admin_message += f"- {item['name']}: {item['quantity']} kg\n"
        if ADMIN_IDS:
            for admin_id_val in ADMIN_IDS: 
                try: await context.bot.send_message(chat_id=admin_id_val, text=admin_message)
                except Exception as e: logger.error(f"Failed to notify admin {admin_id_val}: {e}")
        # Clear cart, preserve language
        current_lang_code = context.user_data.get('language_code') 
        context.user_data.clear(); 
        if current_lang_code: context.user_data['language_code'] = current_lang_code
        # After successful order, send to main menu
        # Need to use update.message or query.message for display_main_menu's target
        # Since this is from a query, query.message is the one to "replace"
        # Create a pseudo update for display_main_menu
        class PseudoUpdate: pass
        pseudo_update = PseudoUpdate()
        pseudo_update.effective_user = user
        pseudo_update.message = query.message # So display_main_menu can send a new message after editing current
        
        # We can't directly edit the message again if display_main_menu sends a new one.
        # Best to send a new message for the main menu here.
        temp_update_for_main_menu = Update(update_id=0) # Dummy update object
        temp_update_for_main_menu.effective_user = user
        temp_update_for_main_menu.message = query.message # This is what display_main_menu will use as target
        await display_main_menu(temp_update_for_main_menu, context, edit_message=False) # Send new message for main menu

    else: # Order failed
        await query.edit_message_text(await _(context, "order_placed_error", user_id=user_id))
        # Offer to go back or view cart
        keyboard = [
            [InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="view_cart_state")],
            [InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="conv_end_to_main_menu")]
        ]
        await query.message.reply_text("What would you like to do?", reply_markup=InlineKeyboardMarkup(keyboard))
        return VIEW_CART_STATE # Allow user to go back to cart or menu

    return ConversationHandler.END


async def my_orders_direct_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: # Standalone, not in a conv by default
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    orders = get_user_orders_from_db(user_id)
    # ... (my_orders display logic from before) ...
    if not orders: message_text = await _(context, "no_orders_yet", user_id=user_id)
    else:
        message_text = await _(context, "my_orders_title", user_id=user_id) + "\n\n"
        for oid, date, total, status, items in orders: 
            message_text += await _(context, "order_details_format", user_id=user_id, order_id=oid, date=date, status=status.capitalize(), total=total, items=items)
    
    keyboard = [[InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="back_to_main_menu_direct")]]
    await query.edit_message_text(text=message_text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END # This handler isn't part of a conv, but if it were, END. Or just return


# --- Admin Flow (simplified for clarity, assuming distinct entry points for now) ---
# ... (admin_panel, admin_add_product_*, admin_manage_products, etc. largely same as before) ...
# Key is how back buttons and transitions are handled.
# For admin, "back_to_admin_panel" callback should end current admin sub-conversation and call admin_panel_display_helper.

async def admin_panel_display_helper(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    user_id = update.effective_user.id
    # Ensure admin
    if not ADMIN_IDS or user_id not in ADMIN_IDS: 
        # This shouldn't be reached if called by admin buttons, but as a safeguard
        await (update.callback_query.message if edit_message and update.callback_query else update.message).reply_text(
            await _(context, "admin_unauthorized", user_id=user_id))
        return ADMIN_PANEL_STATE # Or ConversationHandler.END

    context.chat_data['user_id_for_translation'] = user_id 
    keyboard = [
        [InlineKeyboardButton(await _(context, "admin_add_product_button", user_id=user_id), callback_data="admin_add_prod_entry")],
        [InlineKeyboardButton(await _(context, "admin_manage_products_button", user_id=user_id), callback_data="admin_manage_prod_entry")],
        [InlineKeyboardButton(await _(context, "admin_view_orders_button", user_id=user_id), callback_data="admin_view_orders_direct")],
        [InlineKeyboardButton(await _(context, "admin_shopping_list_button", user_id=user_id), callback_data="admin_shopping_list_direct")],
        [InlineKeyboardButton(await _(context, "admin_exit_button", user_id=user_id), callback_data="admin_exit_direct")], # Exit from Telegram bot sense
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    panel_title = await _(context, "admin_panel_title", user_id=user_id)
    
    target_message = update.callback_query.message if edit_message and update.callback_query else update.message
    if edit_message and target_message:
        try: await target_message.edit_text(panel_title, reply_markup=reply_markup)
        except Exception: await update.message.reply_text(panel_title, reply_markup=reply_markup) # Fallback
    else: await update.message.reply_text(panel_title, reply_markup=reply_markup)
    return ADMIN_PANEL_STATE


async def admin_panel_entry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /admin command."""
    return await admin_panel_display_helper(update, context, edit_message=False)

async def admin_panel_return_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles 'back to admin panel' buttons."""
    if update.callback_query: await update.callback_query.answer()
    return await admin_panel_display_helper(update, context, edit_message=True)


# --- Admin Add Product ---
async def admin_add_product_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (same as admin_add_product_start) ...
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    await query.edit_message_text(text=await _(context, "admin_enter_product_name", user_id=user_id))
    return ADMIN_ADD_PRODUCT_NAME
# ... (admin_add_product_name, admin_add_product_price remain similar, ensure they return to admin panel or end conv) ...
async def admin_add_product_name_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; product_name_typed = update.message.text
    context.user_data['new_product_name'] = product_name_typed
    await update.message.reply_text(await _(context, "admin_enter_product_price", user_id=user_id, product_name=product_name_typed))
    return ADMIN_ADD_PRODUCT_PRICE

async def admin_add_product_price_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; name = context.user_data.get('new_product_name')
    try: price = float(update.message.text); assert price > 0
    except (ValueError, AssertionError):
        await update.message.reply_text(await _(context, "admin_invalid_price", user_id=user_id)); return ADMIN_ADD_PRODUCT_PRICE
    if name is None: # Should not happen if flow is correct
        await update.message.reply_text(await _(context, "generic_error_message", user_id=user_id, default="Error.")); 
        return await admin_panel_return_handler(update, context) # Go back to panel

    if add_product_to_db(name, price): await update.message.reply_text(await _(context, "admin_product_added", user_id=user_id, product_name=name, price=price))
    else: await update.message.reply_text(await _(context, "admin_product_add_failed", user_id=user_id, product_name=name))
    if 'new_product_name' in context.user_data: del context.user_data['new_product_name']
    return await admin_panel_return_handler(update, context) # End this sub-flow, back to admin panel


# --- Admin Manage Products ---
# ... (This will be a conversation: list -> options -> edit_price / toggle / delete_confirm -> delete_do)
# ... (admin_manage_products_entry, admin_product_options_state, admin_edit_price_entry, admin_edit_price_state, etc.)
# ... (admin_toggle_availability_handler, admin_delete_confirm_handler, admin_delete_do_handler)
# These need careful state transitions and "back" buttons within this specific sub-flow.

# For other admin direct actions (View Orders, Shopping List, Exit)
async def admin_view_orders_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (same as admin_view_all_orders but uses admin_panel_return_handler for back button)
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    orders = get_all_orders_from_db()
    # ... (message formatting) ...
    if not orders: message_text = await _(context, "admin_no_orders_found", user_id=user_id)
    else:
        message_text = await _(context, "admin_all_orders_title", user_id=user_id)
        for oid, cuid, uname, date, total, status, items in orders:
            message_text += await _(context, "admin_order_details_format", user_id=user_id, order_id=oid, user_name=uname, customer_id=cuid, date=date, total=total, status=status.capitalize(), items=items)
    if len(message_text) > 4000: message_text = message_text[:4000] + "\n... (truncated)"
    keyboard = [[InlineKeyboardButton(await _(context, "admin_back_to_admin_panel_button", user_id=user_id), callback_data="admin_panel_return")]]
    await query.edit_message_text(text=message_text, reply_markup=InlineKeyboardMarkup(keyboard))
    # This is not part of a conversation, so no state return.

async def admin_shopping_list_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (similar to admin_shopping_list, with back button to admin_panel_return_handler)
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    s_list = get_shopping_list_from_db()
    if not s_list: message_text = await _(context, "admin_shopping_list_empty", user_id=user_id)
    else:
        message_text = await _(context, "admin_shopping_list_title", user_id=user_id)
        for name, qty in s_list: message_text += await _(context, "admin_shopping_list_item_format", user_id=user_id, name=name, total_quantity=qty)
    keyboard = [[InlineKeyboardButton(await _(context, "admin_back_to_admin_panel_button", user_id=user_id), callback_data="admin_panel_return")]]
    await query.edit_message_text(text=message_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_exit_direct_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); user_id = query.from_user.id
    await query.edit_message_text(await _(context, "admin_panel_exit_message", user_id=user_id))
    # This effectively ends the admin interaction. User can /start or /admin again.


async def general_cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # This is a generic cancel that ends the current conversation and shows the main menu.
    user_id = update.effective_user.id
    cancel_text = await _(context, "action_cancelled", user_id=user_id)
    target_message = update.message
    edit_mode = False
    if update.callback_query:
        target_message = update.callback_query.message
        edit_mode = True
        await update.callback_query.answer()

    if edit_mode and target_message:
        try: await target_message.edit_text(cancel_text)
        except Exception: pass # Ignore if cannot edit
    elif target_message: await target_message.reply_text(cancel_text, reply_markup=ReplyKeyboardRemove())
    
    # Preserve language, clear rest of user_data for this conversation
    lang = context.user_data.get('language_code')
    # context.user_data.clear() # Be careful with broad clear, might affect other things
    # For specific conversation, clear its specific keys like 'cart', 'current_product_id' etc.
    # Or rely on starting a new conversation to reset its context.
    if lang: context.user_data['language_code'] = lang
    
    # After cancelling, show the appropriate main menu (user or admin)
    if ADMIN_IDS and user_id in ADMIN_IDS:
        await admin_panel_display_helper(update, context, edit_message=True) # or False if new message preferred
    else:
        await display_main_menu(update, context, edit_message=True) # or False
    return ConversationHandler.END


def main() -> None:
    global ADMIN_IDS 
    if not TELEGRAM_TOKEN or not ADMIN_TELEGRAM_ID:
        logger.critical("TELEGRAM_TOKEN or ADMIN_TELEGRAM_ID not set! Aborting."); return
    try: ADMIN_IDS = [int(aid.strip()) for aid in ADMIN_TELEGRAM_ID.split(',')]
    except ValueError: logger.critical("ADMIN_TELEGRAM_ID invalid! Aborting."); return
    load_translations() 
    if not translations.get("en") or not translations.get("lt"):
        logger.critical("Essential translations missing! Aborting."); return
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # --- Conversation Handlers ---
    # Language Selection Conversation
    lang_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_language_entry, pattern="^set_language_entry$")],
        states={SELECT_LANGUAGE_STATE: [CallbackQueryHandler(language_selected, pattern="^lang_(en|lt)$")]},
        fallbacks=[CallbackQueryHandler(back_to_main_menu_direct_handler, pattern="^back_to_main_menu_direct$"), CommandHandler("cancel", general_cancel_conversation)]
    )

    # User Ordering Conversation
    order_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(browse_products_entry, pattern="^browse_products_entry$")],
        states={
            BROWSE_PRODUCTS_STATE: [ # Now a state to handle product listing and "add more"
                CallbackQueryHandler(product_selected_for_qty, pattern="^prod_\d+$"),
                CallbackQueryHandler(view_cart_state_handler, pattern="^view_cart_state$"),
                CallbackQueryHandler(lambda u,c: browse_products_action(u,c,u.callback_query.from_user.id), pattern="^browse_products_state_return$"), # Add more
            ],
            SELECTING_PRODUCT_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_typed_for_cart)],
            VIEW_CART_STATE: [ # State for when cart is displayed
                CallbackQueryHandler(remove_item_from_cart_handler, pattern="^remove_item_\d+$"),
                CallbackQueryHandler(checkout_order_handler, pattern="^checkout_order$"),
                CallbackQueryHandler(lambda u,c: browse_products_action(u,c,u.callback_query.from_user.id), pattern="^browse_products_state_return$"), # Add more from cart
            ]
        },
        fallbacks=[
            CallbackQueryHandler(back_to_main_menu_direct_handler, pattern="^conv_end_to_main_menu$"), # Specific "end and go to main"
            CommandHandler("start", start), # Allow restart
            CommandHandler("cancel", general_cancel_conversation)
        ]
    )

    # Admin Add Product Conversation
    admin_add_prod_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_product_entry, pattern="^admin_add_prod_entry$")],
        states={
            ADMIN_ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_name_state)],
            ADMIN_ADD_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_price_state)]
        },
        fallbacks=[
            CallbackQueryHandler(admin_panel_return_handler, pattern="^admin_panel_return$"), # Back to admin panel
            CommandHandler("cancel", general_cancel_conversation) # General cancel
        ]
    )
    
    # Placeholder for Admin Manage Products Conversation (More complex)
    # This would include states for listing, selecting a product, options (edit price, toggle, delete confirm), etc.
    # For now, direct handlers for some parts, or a very simple flow.
    # We need admin_manage_products_entry, admin_product_options_state, etc.
    # This part requires careful planning of states and transitions.

    # --- Register Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(lang_conv)
    application.add_handler(order_conv)

    # Direct access (non-conversational or entry points for simple convs)
    application.add_handler(CallbackQueryHandler(view_cart_state_handler, pattern="^view_cart_direct$")) # From main menu
    application.add_handler(CallbackQueryHandler(my_orders_direct_handler, pattern="^my_orders_direct$")) # From main menu
    application.add_handler(CallbackQueryHandler(back_to_main_menu_direct_handler, pattern="^back_to_main_menu_direct$")) # General back button

    # Admin Handlers
    application.add_handler(CommandHandler("admin", admin_panel_entry_command))
    application.add_handler(admin_add_prod_conv)
    application.add_handler(CallbackQueryHandler(admin_panel_return_handler, pattern="^admin_panel_return$")) # General back for admin sub-menus

    # Admin direct actions (if not part of a conversation yet)
    application.add_handler(CallbackQueryHandler(admin_view_orders_direct, pattern="^admin_view_orders_direct$"))
    application.add_handler(CallbackQueryHandler(admin_shopping_list_direct, pattern="^admin_shopping_list_direct$"))
    application.add_handler(CallbackQueryHandler(admin_exit_direct_handler, pattern="^admin_exit_direct$"))
    
    # The complex Admin Manage Products flow still needs to be fully implemented as a ConversationHandler
    # For now, I'll add a placeholder entry for it.
    # application.add_handler(admin_manage_products_conv) # You'd define this similar to order_conv


    logger.info("Bot starting...")
    application.run_polling()

if __name__ == "__main__": main()
