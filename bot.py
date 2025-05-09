import logging
import sqlite3
import os
import json # For loading language files
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

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "lt") # Default to Lithuanian

# --- Language/Localization Setup ---
translations = {}
ADMIN_IDS = [] # Will be populated in main()

def load_translations():
    global translations
    for lang_code in ["en", "lt"]: # Add more languages here if needed
        try:
            # Assuming locales folder is in the same directory as bot.py
            script_dir = os.path.dirname(__file__) #<-- get script directory
            file_path = os.path.join(script_dir, "locales", f"{lang_code}.json")
            with open(file_path, "r", encoding="utf-8") as f:
                translations[lang_code] = json.load(f)
            logger.info(f"Successfully loaded translation file: {file_path}")
        except FileNotFoundError:
            logger.error(f"Translation file for {lang_code}.json not found at {file_path}")
        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON from {lang_code}.json at {file_path}")
    if not translations.get("en") or not translations.get("lt"): # Check specifically for en and lt
        logger.error("Essential English or Lithuanian translation files are missing. Bot might not work correctly.")


async def get_user_language(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    if 'language_code' in context.user_data:
        return context.user_data['language_code']
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT language_code FROM users WHERE telegram_id = ?", (user_id,))
        result = cursor.fetchone()
    except sqlite3.Error as e:
        logger.error(f"Database error in get_user_language for user {user_id}: {e}")
        result = None # Ensure result is defined
    finally:
        conn.close()
    
    if result and result[0]:
        context.user_data['language_code'] = result[0]
        return result[0]
    
    context.user_data['language_code'] = DEFAULT_LANGUAGE 
    return DEFAULT_LANGUAGE

async def _(context: ContextTypes.DEFAULT_TYPE, key: str, user_id: int = None, **kwargs) -> str:
    """Helper to get translated string."""
    actual_user_id_for_lang = None
    if user_id is not None:
        actual_user_id_for_lang = user_id
    elif context.effective_user: 
        actual_user_id_for_lang = context.effective_user.id
    elif 'user_id_for_translation' in context.chat_data: 
        actual_user_id_for_lang = context.chat_data['user_id_for_translation']

    lang_code = DEFAULT_LANGUAGE 
    if actual_user_id_for_lang:
        lang_code = await get_user_language(context, actual_user_id_for_lang)
    
    # Ensure translations for the lang_code exist, otherwise fallback
    lang_translations = translations.get(lang_code, translations.get(DEFAULT_LANGUAGE, translations.get("en", {})))


    text_to_return = lang_translations.get(key)
    
    if text_to_return is None: # If key not in chosen lang, try default, then English
        if lang_code != DEFAULT_LANGUAGE:
            text_to_return = translations.get(DEFAULT_LANGUAGE, {}).get(key)
    if text_to_return is None:
        if lang_code != "en" and DEFAULT_LANGUAGE != "en": # Avoid double check if default is 'en'
             text_to_return = translations.get("en", {}).get(key)
    if text_to_return is None: # Final fallback to key itself
        logger.warning(f"Translation key '{key}' not found in '{lang_code}', default, or 'en'. Using key itself.")
        text_to_return = key
    
    try:
        return text_to_return.format(**kwargs)
    except KeyError as e: 
        logger.warning(f"Missing placeholder {e} for key '{key}' in lang '{lang_code}'. Kwargs: {kwargs}. String: '{text_to_return}'")
        return text_to_return 
    except Exception as e:
        logger.error(f"Error formatting string for key '{key}': {e}")
        return key

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_NAME = "bot.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    sql_create_users_table = f"""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        first_name TEXT,
        username TEXT,
        is_admin INTEGER DEFAULT 0,
        language_code TEXT DEFAULT '{DEFAULT_LANGUAGE}'
    )
    """
    cursor.execute(sql_create_users_table)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        price_per_kg REAL NOT NULL,
        is_available INTEGER DEFAULT 1 
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        user_name TEXT,
        order_date TEXT NOT NULL,
        total_price REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        FOREIGN KEY (user_id) REFERENCES users (telegram_id)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity_kg REAL NOT NULL,
        price_at_order REAL NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders (id),
        FOREIGN KEY (product_id) REFERENCES products (id)
    )
    """)
    conn.commit()
    conn.close()

async def ensure_user_exists(user_id: int, first_name: str, username: str, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    is_admin_user = 1 if ADMIN_IDS and user_id in ADMIN_IDS else 0 # Check if ADMIN_IDS is populated

    try:
        cursor.execute("SELECT language_code FROM users WHERE telegram_id = ?", (user_id,))
        user_record = cursor.fetchone()

        current_lang = DEFAULT_LANGUAGE
        if user_record and user_record[0]:
            current_lang = user_record[0]
        
        context.user_data['language_code'] = current_lang

        cursor.execute("""
            INSERT INTO users (telegram_id, first_name, username, language_code, is_admin) 
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
            first_name = excluded.first_name,
            username = excluded.username,
            is_admin = excluded.is_admin,
            language_code = COALESCE(users.language_code, excluded.language_code) 
        """, (user_id, first_name, username, current_lang, is_admin_user)) 
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error in ensure_user_exists for user {user_id}: {e}")
        current_lang = DEFAULT_LANGUAGE # Fallback
    finally:
        conn.close()
    return current_lang


async def set_user_language_db(user_id: int, lang_code: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET language_code = ? WHERE telegram_id = ?", (lang_code, user_id))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error in set_user_language_db for user {user_id}: {e}")
    finally:
        conn.close()

def add_product_to_db(name, price):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO products (name, price_per_kg) VALUES (?, ?)", (name, price))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    except sqlite3.Error as e:
        logger.error(f"DB error adding product {name}: {e}")
        return False
    finally:
        conn.close()

def get_products_from_db(available_only=True):
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

def get_product_by_id(product_id):
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

def update_product_in_db(product_id, name=None, price=None, is_available=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    success = False
    try:
        fields = []
        params = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if price is not None:
            fields.append("price_per_kg = ?")
            params.append(price)
        if is_available is not None:
            fields.append("is_available = ?")
            params.append(is_available)
        
        if not fields:
            return False

        params.append(product_id)
        query = f"UPDATE products SET {', '.join(fields)} WHERE id = ?"
        cursor.execute(query, tuple(params))
        conn.commit()
        success = True
    except sqlite3.Error as e:
        logger.error(f"Error updating product {product_id}: {e}")
    finally:
        conn.close()
    return success

def delete_product_from_db(product_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    success = False
    try:
        cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        success = True
    except sqlite3.Error as e:
        logger.error(f"Error deleting product {product_id}: {e}")
    finally:
        conn.close()
    return success

def save_order_to_db(user_id, user_name, cart, total_price):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    order_id = None
    order_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute("INSERT INTO orders (user_id, user_name, order_date, total_price) VALUES (?, ?, ?, ?)",
                       (user_id, user_name, order_date, total_price))
        order_id = cursor.lastrowid
        for item in cart:
            cursor.execute("INSERT INTO order_items (order_id, product_id, quantity_kg, price_at_order) VALUES (?, ?, ?, ?)",
                           (order_id, item['id'], item['quantity'], item['price']))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Error saving order for user {user_id}: {e}")
        order_id = None # Ensure it's None on error
    finally:
        conn.close()
    return order_id

def get_user_orders_from_db(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    orders = []
    try:
        cursor.execute("""
            SELECT o.id, o.order_date, o.total_price, o.status, group_concat(p.name || ' (' || oi.quantity_kg || 'kg)', ', ') 
            FROM orders o
            JOIN order_items oi ON o.id = oi.order_id
            JOIN products p ON oi.product_id = p.id
            WHERE o.user_id = ?
            GROUP BY o.id
            ORDER BY o.order_date DESC
        """, (user_id,))
        orders = cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error getting orders for user {user_id}: {e}")
    finally:
        conn.close()
    return orders

def get_all_orders_from_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    orders = []
    try:
        cursor.execute("""
            SELECT o.id, o.user_id, o.user_name, o.order_date, o.total_price, o.status,
                   GROUP_CONCAT(p.name || ' (' || oi.quantity_kg || 'kg @ ' || oi.price_at_order || ' EUR)', CHAR(10)) as items_details
            FROM orders o
            JOIN order_items oi ON o.id = oi.order_id
            JOIN products p ON oi.product_id = p.id
            GROUP BY o.id
            ORDER BY o.order_date DESC
        """)
        orders = cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error getting all orders: {e}")
    finally:
        conn.close()
    return orders
    
def get_shopping_list_from_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    shopping_list = []
    try:
        cursor.execute("""
            SELECT p.name, SUM(oi.quantity_kg) as total_quantity
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            JOIN orders o ON oi.order_id = o.id
            WHERE o.status IN ('pending', 'confirmed') 
            GROUP BY p.name
            ORDER BY p.name
        """)
        shopping_list = cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error getting shopping list: {e}")
    finally:
        conn.close()
    return shopping_list

(SELECTING_PRODUCT, TYPING_QUANTITY, ADD_PRODUCT_NAME, ADD_PRODUCT_PRICE,
 EDIT_PRODUCT_SELECT, EDIT_PRODUCT_PRICE, ADMIN_ACTION, SELECTING_LANGUAGE) = range(8)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await ensure_user_exists(user.id, user.first_name or "", user.username or "", context) 
    
    current_lang_code = context.user_data.get('language_code')
    context.user_data.clear() 
    if current_lang_code:
        context.user_data['language_code'] = current_lang_code
    else: # If not in user_data after clear, re-fetch (should be set by ensure_user_exists)
        context.user_data['language_code'] = await get_user_language(context, user.id)


    keyboard = [
        [InlineKeyboardButton(await _(context, "browse_products_button", user_id=user.id), callback_data="browse_products")],
        [InlineKeyboardButton(await _(context, "view_cart_button", user_id=user.id), callback_data="view_cart")],
        [InlineKeyboardButton(await _(context, "my_orders_button", user_id=user.id), callback_data="my_orders")],
        [InlineKeyboardButton(await _(context, "set_language_button", user_id=user.id), callback_data="set_language")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = await _(context, "welcome_message", user_id=user.id, user_mention=user.mention_html())
    await update.message.reply_html(
        welcome_text,
        reply_markup=reply_markup,
    )

async def set_language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    if query: await query.answer()

    keyboard = [
        [InlineKeyboardButton("English 🇬🇧", callback_data="lang_en")],
        [InlineKeyboardButton("Lietuvių 🇱🇹", callback_data="lang_lt")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    prompt_text = await _(context, "choose_language", user_id=user_id)

    target_message = query.message if query else update.message
    await target_message.edit_text(text=prompt_text, reply_markup=reply_markup) if query else await target_message.reply_text(text=prompt_text, reply_markup=reply_markup)
    return SELECTING_LANGUAGE


async def language_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang_code = query.data.split('_')[1]  
    user_id = query.from_user.id

    context.user_data['language_code'] = lang_code
    await set_user_language_db(user_id, lang_code)

    lang_name = "English" if lang_code == "en" else "Lietuvių"
    confirmation_text = await _(context, "language_set_to", user_id=user_id, language_name=lang_name) 
    
    user = query.effective_user
        
    keyboard = [
        [InlineKeyboardButton(await _(context, "browse_products_button", user_id=user_id), callback_data="browse_products")],
        [InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="view_cart")],
        [InlineKeyboardButton(await _(context, "my_orders_button", user_id=user_id), callback_data="my_orders")],
        [InlineKeyboardButton(await _(context, "set_language_button", user_id=user_id), callback_data="set_language")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = await _(context, "welcome_message", user_id=user_id, user_mention=user.mention_html())
    
    await query.edit_message_text(
        text=f"{confirmation_text}\n\n{welcome_text}", 
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    return ConversationHandler.END

async def browse_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    
    products = get_products_from_db(available_only=True)
    if not products:
        await query.edit_message_text(text=await _(context, "no_products_available", user_id=user_id))
        return ConversationHandler.END 

    keyboard = []
    for prod_id, name, price, _ in products:
        keyboard.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR/kg", callback_data=f"prod_{prod_id}")])
    keyboard.append([InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text=await _(context, "products_title", user_id=user_id), reply_markup=reply_markup)
    return SELECTING_PRODUCT

async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    product_id_str = query.data.split('_')[1]
    
    try:
        product_id = int(product_id_str)
    except ValueError:
        logger.error(f"Invalid product_id in callback_data: {query.data}")
        await query.edit_message_text(text=await _(context, "generic_error_message", user_id=user_id, default="An error occurred. Please try again.")) 
        return SELECTING_PRODUCT

    product = get_product_by_id(product_id)
    if not product:
        await query.edit_message_text(text=await _(context, "product_not_found", user_id=user_id, default="Product not found.")) 
        return SELECTING_PRODUCT

    context.user_data['current_product_id'] = product_id
    context.user_data['current_product_name'] = product[1] 
    context.user_data['current_product_price'] = product[2]

    prompt_text = await _(context, "product_selected_prompt", user_id=user_id, product_name=product[1])
    await query.edit_message_text(text=prompt_text)
    return TYPING_QUANTITY

async def quantity_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text
    user_id = update.effective_user.id
    try:
        quantity = float(user_input)
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
    except ValueError:
        await update.message.reply_text(await _(context, "invalid_quantity_prompt", user_id=user_id))
        return TYPING_QUANTITY

    product_id = context.user_data['current_product_id']
    product_name = context.user_data['current_product_name']
    product_price = context.user_data['current_product_price']

    if 'cart' not in context.user_data:
        context.user_data['cart'] = []

    found = False
    for item in context.user_data['cart']:
        if item['id'] == product_id:
            item['quantity'] += quantity
            found = True
            break
    if not found:
        context.user_data['cart'].append({'id': product_id, 'name': product_name, 'price': product_price, 'quantity': quantity})
    
    await update.message.reply_text(await _(context, "item_added_to_cart", user_id=user_id, quantity=quantity, product_name=product_name))
    
    keyboard = [
        [InlineKeyboardButton(await _(context, "add_more_products_button", user_id=user_id), callback_data="browse_products_again")],
        [InlineKeyboardButton(await _(context, "view_cart_and_checkout_button", user_id=user_id), callback_data="view_cart")],
        [InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="main_menu_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(await _(context, "what_next_prompt", user_id=user_id), reply_markup=reply_markup)
    return SELECTING_PRODUCT

async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    if query: await query.answer()
    
    cart = context.user_data.get('cart', [])
    if not cart:
        message = await _(context, "cart_empty", user_id=user_id)
        keyboard = [[InlineKeyboardButton(await _(context, "browse_products_button", user_id=user_id), callback_data="browse_products_again")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        target_message = query.message if query else update.message
        await target_message.edit_text(text=message, reply_markup=reply_markup) if query else await target_message.reply_text(text=message, reply_markup=reply_markup)
        return SELECTING_PRODUCT # Or ConversationHandler.END if appropriate

    cart_summary = await _(context, "your_cart_title", user_id=user_id) + "\n"
    total_price = 0
    remove_buttons = []
    for i, item in enumerate(cart):
        item_total = item['price'] * item['quantity']
        cart_summary += f"{i+1}. {item['name']} - {item['quantity']} kg x {item['price']:.2f} EUR = {item_total:.2f} EUR\n"
        total_price += item_total
        remove_buttons.append(InlineKeyboardButton(await _(context, "remove_item_button", user_id=user_id, item_index=i+1), callback_data=f"remove_{i}"))
    
    cart_summary += "\n" + await _(context, "cart_total", user_id=user_id, total_price=total_price)

    keyboard = []
    for i in range(0, len(remove_buttons), 3): 
        keyboard.append(remove_buttons[i:i+3])

    keyboard.extend([
        [InlineKeyboardButton(await _(context, "checkout_button", user_id=user_id), callback_data="checkout")],
        [InlineKeyboardButton(await _(context, "add_more_products_button", user_id=user_id), callback_data="browse_products_again")],
        [InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="main_menu_action")]
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)

    target_message = query.message if query else update.message
    await target_message.edit_text(text=cart_summary, reply_markup=reply_markup) if query else await target_message.reply_text(text=cart_summary, reply_markup=reply_markup)
    return SELECTING_PRODUCT

async def remove_item_from_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    
    item_index_to_remove = int(query.data.split('_')[1]) 
    cart = context.user_data.get('cart', [])

    removed_item_name = "Unknown item"
    if 0 <= item_index_to_remove < len(cart):
        removed_item = cart.pop(item_index_to_remove)
        removed_item_name = removed_item['name']
        await query.message.reply_text(await _(context, "item_removed_from_cart", user_id=user_id, item_name=removed_item_name))
    else:
        await query.message.reply_text(await _(context, "invalid_item_to_remove", user_id=user_id))
    
    return await view_cart(update, context) 


async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    cart = context.user_data.get('cart', [])
    if not cart:
        await query.edit_message_text(await _(context, "cart_empty", user_id=user_id))
        return SELECTING_PRODUCT

    user = query.effective_user
    total_price = sum(item['price'] * item['quantity'] for item in cart)
    
    order_id = save_order_to_db(user.id, user.full_name or "", cart, total_price)

    if order_id:
        await query.edit_message_text(
            await _(context, "order_placed_success", user_id=user_id, order_id=order_id, total_price=total_price)
        )
        admin_message = f"🔔 New Order #{order_id} from {user.full_name or 'N/A'} (@{user.username or 'N/A'}, ID: {user.id})\nTotal: {total_price:.2f} EUR\nItems:\n"
        for item in cart:
            admin_message += f"- {item['name']}: {item['quantity']} kg\n"
        
        for admin_id_val in ADMIN_IDS: 
            try:
                await context.bot.send_message(chat_id=admin_id_val, text=admin_message)
            except Exception as e:
                logger.error(f"Failed to send new order notification to admin {admin_id_val}: {e}")
        
        current_lang_code = context.user_data.get('language_code') 
        context.user_data.clear() 
        if current_lang_code: context.user_data['language_code'] = current_lang_code
    else:
        await query.edit_message_text(await _(context, "order_placed_error", user_id=user_id))
    
    return ConversationHandler.END


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    if query: await query.answer()

    orders = get_user_orders_from_db(user_id)
    if not orders:
        message_text = await _(context, "no_orders_yet", user_id=user_id)
    else:
        message_text = await _(context, "my_orders_title", user_id=user_id) + "\n\n"
        for order_id_db, date, total, status, items in orders: # Renamed order_id to order_id_db
            status_translated = status.capitalize() 
            message_text += await _(context, "order_details_format", user_id=user_id,
                                    order_id=order_id_db, date=date, status=status_translated, 
                                    total=total, items=items)
    
    keyboard = [[InlineKeyboardButton(await _(context, "back_to_main_menu_button", user_id=user_id), callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    target_message = query.message if query else update.message
    await target_message.edit_text(text=message_text, reply_markup=reply_markup) if query else await target_message.reply_text(text=message_text, reply_markup=reply_markup)


async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: # Should always be called from a query
        logger.warning("back_to_main_menu called without a query.")
        # Attempt to use update.effective_user if available as a fallback
        user = update.effective_user
        if not user:
            # Cannot proceed without user context
            return ConversationHandler.END
        user_id = user.id
        # Send a new message instead of editing
        keyboard = [
            [InlineKeyboardButton(await _(context, "browse_products_button", user_id=user_id), callback_data="browse_products")],
            [InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="view_cart")],
            [InlineKeyboardButton(await _(context, "my_orders_button", user_id=user_id), callback_data="my_orders")],
            [InlineKeyboardButton(await _(context, "set_language_button", user_id=user_id), callback_data="set_language")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        welcome_text = await _(context, "welcome_message", user_id=user_id, user_mention=user.mention_html() if hasattr(user, 'mention_html') else user.full_name)
        title_text = await _(context, "main_menu_title", user_id=user_id)
        await update.message.reply_html(f"{title_text}\n{welcome_text}", reply_markup=reply_markup)
        return ConversationHandler.END

    user = query.effective_user 
    user_id = user.id
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton(await _(context, "browse_products_button", user_id=user_id), callback_data="browse_products")],
        [InlineKeyboardButton(await _(context, "view_cart_button", user_id=user_id), callback_data="view_cart")],
        [InlineKeyboardButton(await _(context, "my_orders_button", user_id=user_id), callback_data="my_orders")],
        [InlineKeyboardButton(await _(context, "set_language_button", user_id=user_id), callback_data="set_language")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = await _(context, "welcome_message", user_id=user_id, user_mention=user.mention_html())
    title_text = await _(context, "main_menu_title", user_id=user_id)
    
    await query.edit_message_text(
        f"{title_text}\n{welcome_text}", 
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    return ConversationHandler.END 

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    context.chat_data['user_id_for_translation'] = user_id 

    if not ADMIN_IDS or user_id not in ADMIN_IDS: # Check if ADMIN_IDS is populated
        await update.message.reply_text(await _(context, "admin_unauthorized", user_id=user_id)) 
        return

    keyboard = [
        [InlineKeyboardButton(await _(context, "admin_add_product_button", user_id=user_id), callback_data="admin_add_prod")],
        [InlineKeyboardButton(await _(context, "admin_manage_products_button", user_id=user_id), callback_data="admin_manage_prod")],
        [InlineKeyboardButton(await _(context, "admin_view_orders_button", user_id=user_id), callback_data="admin_view_orders")],
        [InlineKeyboardButton(await _(context, "admin_shopping_list_button", user_id=user_id), callback_data="admin_shopping_list")],
        [InlineKeyboardButton(await _(context, "admin_exit_button", user_id=user_id), callback_data="admin_exit")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(await _(context, "admin_panel_title", user_id=user_id), reply_markup=reply_markup)

async def admin_add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    await query.edit_message_text(text=await _(context, "admin_enter_product_name", user_id=user_id))
    return ADD_PRODUCT_NAME

async def admin_add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    context.user_data['new_product_name'] = update.message.text
    prompt_text = await _(context, "admin_enter_product_price", user_id=user_id, product_name=update.message.text)
    await update.message.reply_text(prompt_text)
    return ADD_PRODUCT_PRICE

async def admin_add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        price = float(update.message.text)
        if price <= 0:
            raise ValueError("Price must be positive.")
    except ValueError:
        await update.message.reply_text(await _(context, "admin_invalid_price", user_id=user_id))
        return ADD_PRODUCT_PRICE

    name = context.user_data['new_product_name']
    if add_product_to_db(name, price):
        await update.message.reply_text(await _(context, "admin_product_added", user_id=user_id, product_name=name, price=price))
    else:
        await update.message.reply_text(await _(context, "admin_product_add_failed", user_id=user_id, product_name=name))
    
    if 'new_product_name' in context.user_data: del context.user_data['new_product_name']
    await admin_panel_button_handler(update, context)
    return ConversationHandler.END

async def admin_manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    products = get_products_from_db(available_only=False)
    if not products:
        await query.edit_message_text(text=await _(context, "admin_no_products_to_manage", user_id=user_id))
        # Return to admin panel or end conversation if this is a state
        # For now, just return an admin action state that expects further interaction or a back button.
        # Consider adding a "Back to Admin Panel" button here if no products.
        return ADMIN_ACTION 

    keyboard = []
    for prod_id, name, price, available in products:
        status_key = "admin_status_available" if available else "admin_status_unavailable" 
        status_text = await _(context, status_key, user_id=user_id, default= ("Available" if available else "Unavailable"))
        keyboard.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR ({status_text})", callback_data=f"admin_edit_{prod_id}")])
    keyboard.append([InlineKeyboardButton(await _(context, "admin_back_to_admin_panel_button", user_id=user_id), callback_data="admin_main_panel_return")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text=await _(context, "admin_select_product_to_manage", user_id=user_id), reply_markup=reply_markup)
    return ADMIN_ACTION # This state should ideally handle callbacks for product selection or "back"

async def admin_edit_product_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    product_id = int(query.data.split('_')[2]) 
    context.user_data['editing_product_id'] = product_id

    product = get_product_by_id(product_id)
    if not product:
        await query.edit_message_text(await _(context, "admin_product_not_found", user_id=user_id))
        return ADMIN_ACTION # Or back to manage products

    prod_name, prod_price, is_available = product[1], product[2], product[3]
    availability_action_key = "admin_set_unavailable_button" if is_available else "admin_set_available_button"
    availability_action_text = await _(context, availability_action_key, user_id=user_id)

    keyboard = [
        [InlineKeyboardButton(await _(context, "admin_change_price_button", user_id=user_id, price=prod_price), callback_data=f"admin_change_price_{product_id}")],
        [InlineKeyboardButton(availability_action_text, callback_data=f"admin_toggle_avail_{product_id}_{1-is_available}")],
        [InlineKeyboardButton(await _(context, "admin_delete_product_button", user_id=user_id), callback_data=f"admin_delete_confirm_{product_id}")],
        [InlineKeyboardButton(await _(context, "admin_back_to_product_list_button", user_id=user_id), callback_data="admin_manage_prod")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(await _(context, "admin_managing_product", user_id=user_id, product_name=prod_name), reply_markup=reply_markup)
    return ADMIN_ACTION # This state should handle these button callbacks

async def admin_change_product_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    product_id = int(query.data.split('_')[3]) 
    context.user_data['editing_product_id'] = product_id
    product = get_product_by_id(product_id)
    if not product: # Handle case where product might have been deleted
        await query.edit_message_text(await _(context, "admin_product_not_found", user_id=user_id))
        return ADMIN_ACTION # Or back to manage list
    prompt = await _(context, "admin_enter_new_price", user_id=user_id, product_name=product[1], current_price=product[2])
    await query.edit_message_text(prompt)
    return EDIT_PRODUCT_PRICE

async def admin_change_product_price_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        new_price = float(update.message.text)
        if new_price <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text(await _(context, "admin_invalid_price", user_id=user_id))
        return EDIT_PRODUCT_PRICE # Stay in state

    product_id = context.user_data.get('editing_product_id')
    if product_id is None:
        logger.error("editing_product_id not found in user_data for admin_change_product_price_finish")
        await update.message.reply_text(await _(context, "generic_error_message", user_id=user_id, default="An error occurred."))
        await admin_panel_button_handler(update, context) # Go back to admin panel
        return ConversationHandler.END

    if update_product_in_db(product_id, price=new_price):
        await update.message.reply_text(await _(context, "admin_price_updated", user_id=user_id, product_id=product_id))
    else:
        await update.message.reply_text(await _(context, "admin_price_update_failed", user_id=user_id))
    
    if 'editing_product_id' in context.user_data: del context.user_data['editing_product_id']
    await admin_manage_products_button_handler(update, context) # Show product list again
    return ConversationHandler.END

async def admin_toggle_availability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    parts = query.data.split('_') 
    product_id = int(parts[3])
    new_availability = int(parts[4]) 

    if update_product_in_db(product_id, is_available=new_availability):
        status_text_key = "admin_status_available_text" if new_availability else "admin_status_unavailable_text" 
        status_text = await _(context, status_text_key, user_id=user_id, default=("available" if new_availability else "unavailable"))
        await query.edit_message_text(await _(context, "admin_product_set_status", user_id=user_id, product_id=product_id, status_text=status_text))
    else:
        await query.edit_message_text(await _(context, "admin_status_update_failed", user_id=user_id))
    
    # Refresh the product options view or manage products list
    # For simplicity, go back to the "manage products" list view
    await admin_manage_products_button_handler(update, context, query_for_edit=query)
    return ADMIN_ACTION # Stay in admin action state, expecting further choices or back


async def admin_delete_product_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    product_id_str = query.data.split('_')[3]
    try:
        product_id = int(product_id_str)
    except ValueError:
        logger.error(f"Invalid product_id in admin_delete_product_confirm: {product_id_str}")
        await query.edit_message_text(await _(context, "generic_error_message", user_id=user_id, default="Error processing request."))
        return ADMIN_ACTION

    product = get_product_by_id(product_id)
    if not product:
        await query.edit_message_text(await _(context, "admin_product_not_found", user_id=user_id))
        return ADMIN_ACTION # Or back to product list
    
    keyboard = [
        [InlineKeyboardButton(await _(context, "admin_confirm_delete_yes_button",user_id=user_id, product_name=product[1]), callback_data=f"admin_delete_do_{product_id}")],
        [InlineKeyboardButton(await _(context, "admin_confirm_delete_no_button", user_id=user_id), callback_data=f"admin_edit_{product_id}")], 
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(await _(context, "admin_confirm_delete_prompt",user_id=user_id, product_name=product[1]), reply_markup=reply_markup)
    return ADMIN_ACTION # Stay in state, buttons will trigger next action

async def admin_delete_product_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    product_id_str = query.data.split('_')[3] 
    try:
        product_id = int(product_id_str)
    except ValueError:
        logger.error(f"Invalid product_id in admin_delete_product_do: {product_id_str}")
        await query.edit_message_text(await _(context, "generic_error_message", user_id=user_id, default="Error processing request."))
        return ADMIN_ACTION # Or back to product list

    if delete_product_from_db(product_id):
        await query.edit_message_text(await _(context, "admin_product_deleted",user_id=user_id, product_id=product_id))
    else:
        await query.edit_message_text(await _(context, "admin_product_delete_failed", user_id=user_id))
    
    await admin_manage_products_button_handler(update, context, query_for_edit=query)
    return ADMIN_ACTION # Stay in state, or end if this is the final action for this flow


async def admin_view_all_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Determine the user_id for translation (admin's ID)
    user_id_for_translation = query.from_user.id if query else update.effective_user.id
    if query: await query.answer()

    orders = get_all_orders_from_db()
    if not orders:
        message_text = await _(context, "admin_no_orders_found", user_id=user_id_for_translation)
    else:
        message_text = await _(context, "admin_all_orders_title", user_id=user_id_for_translation)
        for order_id_val, customer_user_id_val, user_name_val, date_val, total_val, status_val, items_val in orders:
            status_display = status_val.capitalize() 
            # Ensure your "admin_order_details_format" in JSON uses {customer_id} for the customer's user ID
            message_text += await _(context, 
                                   "admin_order_details_format", 
                                   user_id=user_id_for_translation,  # This is for the _ function to pick admin's language
                                   order_id=order_id_val, 
                                   user_name=user_name_val, 
                                   customer_id=customer_user_id_val, # This is the placeholder for the customer's ID
                                   date=date_val, 
                                   total=total_val, 
                                   status=status_display, 
                                   items=items_val)
    
    if len(message_text) > 4000: 
        message_text = message_text[:4000] + "\n... (message truncated)"

    keyboard = [[InlineKeyboardButton(await _(context, "admin_back_to_admin_panel_button", user_id=user_id_for_translation), callback_data="admin_main_panel_return")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    target_message = query.message if query else update.message
    await target_message.edit_text(text=message_text, reply_markup=reply_markup) if query else await target_message.reply_text(text=message_text, reply_markup=reply_markup)


async def admin_shopping_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    if query: await query.answer()

    shopping_list = get_shopping_list_from_db()
    if not shopping_list:
        message_text = await _(context, "admin_shopping_list_empty", user_id=user_id)
    else:
        message_text = await _(context, "admin_shopping_list_title", user_id=user_id)
        for name, total_quantity in shopping_list:
            # Assuming admin_shopping_list_item_format is like: "- {name}: {total_quantity:.2f} kg\n"
            message_text += await _(context, "admin_shopping_list_item_format", user_id=user_id, name=name, total_quantity=total_quantity)
    
    keyboard = [[InlineKeyboardButton(await _(context, "admin_back_to_admin_panel_button", user_id=user_id), callback_data="admin_main_panel_return")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    target_message = query.message if query else update.message
    await target_message.edit_text(text=message_text, reply_markup=reply_markup) if query else await target_message.reply_text(text=message_text, reply_markup=reply_markup)


async def admin_exit_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    await query.edit_message_text(await _(context, "admin_panel_exit_message", user_id=user_id))
    return ConversationHandler.END # Or a specific state if exiting only part of admin flow


async def admin_panel_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, callback_data_prefix: str = None):
    effective_user = update.effective_user
    if update.callback_query:
        effective_user = update.callback_query.from_user
    
    if not effective_user:
        logger.error("admin_panel_button_handler: Cannot determine effective_user.")
        # Attempt to send a message to a default admin if possible
        if ADMIN_IDS:
            await context.bot.send_message(chat_id=ADMIN_IDS[0], text="An error occurred. Please use /admin again.")
        return

    # Reconstruct a mock update object that admin_panel can use
    class MockMessageForAdminPanel:
        async def reply_text(self, text, reply_markup):
            if update.callback_query and update.callback_query.message:
                return await update.callback_query.message.edit_text(text=text, reply_markup=reply_markup)
            elif update.message : # If original was a message
                 return await update.message.reply_text(text=text, reply_markup=reply_markup)
            else: # Fallback if no original message to edit/reply
                return await context.bot.send_message(chat_id=effective_user.id, text=text, reply_markup=reply_markup)

    mock_update_for_panel = type('MockUpdate', (), {
        'effective_user': effective_user,
        'message': MockMessageForAdminPanel() # admin_panel uses update.message.reply_text
    })()
    await admin_panel(mock_update_for_panel, context)


async def admin_manage_products_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, query_for_edit=None):
    effective_user_to_use = None
    message_target_for_edit = None # This will be the message object we try to edit

    if query_for_edit and hasattr(query_for_edit, 'message'): 
        effective_user_to_use = query_for_edit.from_user
        message_target_for_edit = query_for_edit.message
    elif update.callback_query: 
        effective_user_to_use = update.callback_query.from_user
        message_target_for_edit = update.callback_query.message
    elif update.message: 
        effective_user_to_use = update.effective_user
        # If original was a message, we can't edit it directly with admin_manage_products's structure
        # We'll send a new message in this case via the mock query's edit_message_text
        message_target_for_edit = update.message 
    
    if not effective_user_to_use:
        logger.error("admin_manage_products_button_handler: Could not determine effective_user.")
        if ADMIN_IDS:
            await context.bot.send_message(chat_id=ADMIN_IDS[0], text="Error. Use /admin, then 'Manage Products'.")
        return

    class MockQueryForManageProducts:
        def __init__(self, user, original_msg_obj):
            self.from_user = user
            self.message = original_msg_obj # This is the telegram.Message object

        async def answer(self): pass 
        
        async def edit_message_text(self, text, reply_markup):
            # admin_manage_products calls query.edit_message_text
            if self.message and hasattr(self.message, 'edit_text'):
                try:
                    return await self.message.edit_text(text=text, reply_markup=reply_markup)
                except Exception as e: # If original message was deleted or can't be edited
                    logger.warning(f"Failed to edit message in MockQueryForManageProducts, sending new: {e}")
                    return await context.bot.send_message(chat_id=self.from_user.id, text=text, reply_markup=reply_markup)
            else: # Fallback if no message or can't edit (e.g., if self.message was from update.message)
                logger.info("MockQueryForManageProducts: No message to edit, sending new message.")
                return await context.bot.send_message(chat_id=self.from_user.id, text=text, reply_markup=reply_markup)

    mock_update_obj = type('MockUpdate', (), {
        'callback_query': MockQueryForManageProducts(effective_user_to_use, message_target_for_edit),
        'effective_user': effective_user_to_use # admin_manage_products might use this directly too
    })()
    await admin_manage_products(mock_update_obj, context)


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    cancel_text = await _(context, "action_cancelled", user_id=user_id)
    
    target_message = update.message
    edit_mode = False
    if update.callback_query:
        target_message = update.callback_query.message
        edit_mode = True
        await update.callback_query.answer()

    if edit_mode:
        await target_message.edit_text(cancel_text)
    else:
        await target_message.reply_text(cancel_text, reply_markup=ReplyKeyboardRemove())
    
    current_lang_code = context.user_data.get('language_code')
    context.user_data.clear()
    if current_lang_code: context.user_data['language_code'] = current_lang_code
    
    if ADMIN_IDS and user_id in ADMIN_IDS:
        await admin_panel_button_handler(update, context) 
    else:
        # For regular user, back to main menu.
        # back_to_main_menu expects a query. If not a query, user can /start.
        if update.callback_query:
            await back_to_main_menu(update, context)
        # Else, the cancel message is sent, user can /start again.
            
    return ConversationHandler.END

def main() -> None:
    """Start the bot."""
    global ADMIN_IDS # Ensure ADMIN_IDS is treated as global for modification here
    
    if not TELEGRAM_TOKEN:
        logger.critical("TELEGRAM_TOKEN not set in environment variables! Aborting.")
        return
    if not ADMIN_TELEGRAM_ID:
        logger.critical("ADMIN_TELEGRAM_ID not set in environment variables! Aborting.")
        return
        
    try:
        ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_TELEGRAM_ID.split(',')]
        logger.info(f"Admin IDs loaded: {ADMIN_IDS}")
    except ValueError:
        logger.critical("ADMIN_TELEGRAM_ID is not a valid comma-separated list of numbers! Aborting.")
        return 

    load_translations() 
    if not translations.get("en") or not translations.get("lt"):
        logger.critical("Essential translation files (en.json, lt.json) not loaded. Aborting after attempting to load.")
        return # Check after attempting to load
        
    init_db()

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # --- Define Conversation Handlers ---
    language_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_language_command, pattern="^set_language$")],
        states={
            SELECTING_LANGUAGE: [
                CallbackQueryHandler(language_selected, pattern="^lang_(en|lt)$")
            ]
        },
        fallbacks=[
            CommandHandler("start", start), 
            CallbackQueryHandler(back_to_main_menu, pattern="^main_menu$"), 
            CommandHandler("cancel", cancel_conversation), # General cancel
        ],
        map_to_parent={ ConversationHandler.END: ConversationHandler.END } # Important for nested convs if any
    )

    order_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(browse_products, pattern="^browse_products$"),
            CallbackQueryHandler(browse_products, pattern="^browse_products_again$"),
        ],
        states={
            SELECTING_PRODUCT: [
                CallbackQueryHandler(product_selected, pattern="^prod_\d+$"),
                CallbackQueryHandler(view_cart, pattern="^view_cart$"), # From within the flow
                CallbackQueryHandler(checkout, pattern="^checkout$"),
                CallbackQueryHandler(remove_item_from_cart, pattern="^remove_\d+$"),
                CallbackQueryHandler(back_to_main_menu, pattern="^main_menu_action$"), # Specific back from order flow
            ],
            TYPING_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_typed)],
        },
        fallbacks=[
            CommandHandler("start", start), # Restart conversation
            CallbackQueryHandler(back_to_main_menu, pattern="^main_menu$"), # Fallback to main menu
            CommandHandler("cancel", cancel_conversation),
            CallbackQueryHandler(cancel_conversation, pattern="^cancel$") # Generic cancel button
        ],
        map_to_parent={ConversationHandler.END: ConversationHandler.END}
    )
    
    add_product_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_product_start, pattern="^admin_add_prod$")],
        states={
            ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_name)],
            ADD_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_price)],
        },
        fallbacks=[
            CommandHandler("admin", admin_panel), # Back to admin panel
            CallbackQueryHandler(cancel_conversation, pattern="^cancel_admin_action$"), # Specific cancel
            CallbackQueryHandler(admin_panel_button_handler, pattern="^admin_main_panel_return$") # Button to return
        ],
        map_to_parent={ConversationHandler.END: ConversationHandler.END}
    )
    
    # Admin Product Management Flow (not a full conversation, but a series of callbacks)
    # This could be structured as a ConversationHandler if it becomes more complex.
    # For now, individual CallbackQueryHandlers under a common state (ADMIN_ACTION) or just direct.
    # The ADMIN_ACTION state is useful if these callbacks modify a shared context for product management.

    edit_price_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_change_product_price_start, pattern="^admin_change_price_\d+$")],
        states={
            EDIT_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_change_product_price_finish)],
        },
        fallbacks=[
            CommandHandler("admin", admin_panel),
            # Callback to go back to product list within this flow:
            CallbackQueryHandler(admin_manage_products_button_handler, pattern="^admin_manage_prod_return$"), # Specific back
            CallbackQueryHandler(cancel_conversation, pattern="^cancel_admin_action$"),
        ],
        map_to_parent={ConversationHandler.END: ConversationHandler.END}
    )
    
    # Admin "Manage Products" flow using ADMIN_ACTION state
    admin_manage_flow_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_manage_products, pattern="^admin_manage_prod$")],
        states={
            ADMIN_ACTION: [
                CallbackQueryHandler(admin_edit_product_options, pattern="^admin_edit_\d+$"),
                CallbackQueryHandler(admin_toggle_availability, pattern="^admin_toggle_avail_\d+_\d$"),
                CallbackQueryHandler(admin_delete_product_confirm, pattern="^admin_delete_confirm_\d+$"),
                CallbackQueryHandler(admin_delete_product_do, pattern="^admin_delete_do_\d+$"),
                # edit_price_conv_handler can be an entry to another conversation from here if needed,
                # or its entry point admin_change_price_\d+ can be caught here.
                # For simplicity, edit_price_conv_handler is separate for now.
                CallbackQueryHandler(admin_panel_button_handler, pattern="^admin_main_panel_return$"), # Back to main admin
            ]
        },
        fallbacks=[
            CommandHandler("admin", admin_panel),
            CallbackQueryHandler(cancel_conversation, pattern="^cancel_admin_action$")
        ],
        map_to_parent={ConversationHandler.END: ConversationHandler.END}
    )


    # --- Register handlers ---
    # Top level commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))

    # Conversation handlers
    application.add_handler(language_conv_handler) 
    application.add_handler(order_conv_handler)
    application.add_handler(add_product_conv_handler)
    application.add_handler(edit_price_conv_handler) # For changing price
    application.add_handler(admin_manage_flow_handler) # For other product management actions

    # Direct callback handlers (mostly for main menu buttons or simple actions not in deep conversations)
    application.add_handler(CallbackQueryHandler(view_cart, pattern="^view_cart$")) 
    application.add_handler(CallbackQueryHandler(my_orders, pattern="^my_orders$"))
    
    # Admin direct actions (if not part of admin_manage_flow_handler or other convs)
    application.add_handler(CallbackQueryHandler(admin_view_all_orders, pattern="^admin_view_orders$"))
    application.add_handler(CallbackQueryHandler(admin_shopping_list, pattern="^admin_shopping_list$"))
    application.add_handler(CallbackQueryHandler(admin_exit_panel, pattern="^admin_exit$"))
    # General return to admin panel (can be used by various "back" buttons in admin area)
    application.add_handler(CallbackQueryHandler(admin_panel_button_handler, pattern="^admin_main_panel_return$"))


    logger.info("Bot starting with multi-language support...")
    application.run_polling()

if __name__ == "__main__":
    main()
