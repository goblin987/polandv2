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

def load_translations():
    global translations
    for lang_code in ["en", "lt"]: # Add more languages here if needed
        try:
            with open(f"locales/{lang_code}.json", "r", encoding="utf-8") as f:
                translations[lang_code] = json.load(f)
        except FileNotFoundError:
            logger.error(f"Translation file for {lang_code}.json not found.")
        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON from {lang_code}.json.")
    if not translations:
        logger.error("No translation files loaded. Bot might not work correctly.")

async def get_user_language(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    if 'language_code' in context.user_data:
        return context.user_data['language_code']
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT language_code FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result and result[0]:
        context.user_data['language_code'] = result[0]
        return result[0]
    
    # If no language set for user, use default and store it
    # (We don't store it here, store it when user makes a choice or on first /start)
    context.user_data['language_code'] = DEFAULT_LANGUAGE 
    return DEFAULT_LANGUAGE

async def _(context: ContextTypes.DEFAULT_TYPE, key: str, user_id: int = None, **kwargs) -> str:
    """Helper to get translated string."""
    if user_id is None and context.effective_user: # Try to get from context if not provided
        user_id = context.effective_user.id
    elif user_id is None and 'user_id_for_translation' in context.chat_data: # Fallback for some cases
        user_id = context.chat_data['user_id_for_translation']


    lang_code = DEFAULT_LANGUAGE # Default if user_id is somehow None
    if user_id:
        lang_code = await get_user_language(context, user_id)

    # Fallback logic: try user's lang, then default lang, then English, then key itself
    text_to_return = translations.get(lang_code, {}).get(key)
    if text_to_return is None: # Try default language if specific lang failed
        text_to_return = translations.get(DEFAULT_LANGUAGE, {}).get(key)
    if text_to_return is None: # Try English as a final fallback for strings
        text_to_return = translations.get("en", {}).get(key, key) # Return key if not found in EN either
    
    try:
        return text_to_return.format(**kwargs)
    except KeyError as e: # Placeholder in string not in kwargs
        logger.warning(f"Missing placeholder {e} for key '{key}' in lang '{lang_code}'. Kwargs: {kwargs}")
        return text_to_return # Return unformatted string
    except Exception as e:
        logger.error(f"Error formatting string for key '{key}': {e}")
        return key # Fallback to key

# Logging, DB_NAME, init_db (needs update) are mostly the same
# ... (Your existing logging and DB_NAME) ...

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_NAME = "bot.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Users table (ADD language_code)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        first_name TEXT,
        username TEXT,
        is_admin INTEGER DEFAULT 0,
        language_code TEXT DEFAULT ? 
    )
    """, (DEFAULT_LANGUAGE,)) # Add default language here

    # Products table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        price_per_kg REAL NOT NULL,
        is_available INTEGER DEFAULT 1 
    )
    """)
    # Orders table
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
    # Order items table
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

# --- Helper function to ensure user exists and has a language ---
async def ensure_user_exists(user_id: int, first_name: str, username: str, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT language_code FROM users WHERE telegram_id = ?", (user_id,))
    user_record = cursor.fetchone()

    current_lang = DEFAULT_LANGUAGE
    if user_record and user_record[0]:
        current_lang = user_record[0]
    
    # Set language in context for immediate use
    context.user_data['language_code'] = current_lang

    cursor.execute("""
        INSERT INTO users (telegram_id, first_name, username, language_code, is_admin) 
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
        first_name = excluded.first_name,
        username = excluded.username,
        language_code = COALESCE(users.language_code, excluded.language_code) 
    """, (user_id, first_name, username, current_lang, 1 if user_id in ADMIN_IDS else 0)) # Set admin status
    conn.commit()
    conn.close()
    return current_lang

async def set_user_language_db(user_id: int, lang_code: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET language_code = ? WHERE telegram_id = ?", (lang_code, user_id))
    conn.commit()
    conn.close()

# --- Database functions remain largely the same, ensure they don't hardcode text for errors ---
# ... (Your existing add_product_to_db, get_products_from_db, etc.) ...
# (Make sure any error messages returned from DB functions are general or handled by calling functions for translation)

# --- Conversation States ---
# (SELECTING_PRODUCT, TYPING_QUANTITY, ADD_PRODUCT_NAME, ADD_PRODUCT_PRICE,
#  EDIT_PRODUCT_SELECT, EDIT_PRODUCT_PRICE, ADMIN_ACTION) = range(7)
# Add one for language selection
(SELECTING_PRODUCT, TYPING_QUANTITY, ADD_PRODUCT_NAME, ADD_PRODUCT_PRICE,
 EDIT_PRODUCT_SELECT, EDIT_PRODUCT_PRICE, ADMIN_ACTION, SELECTING_LANGUAGE) = range(8)


# --- User (Client) Side Handlers (NOW USING _ FOR TRANSLATION) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await ensure_user_exists(user.id, user.first_name, user.username, context) # Ensure user & lang is set
    context.user_data.clear() # Clear any previous user data/cart but keep language_code
    user_lang = await get_user_language(context, user.id) # Re-fetch to be sure it's in user_data
    context.user_data['language_code'] = user_lang


    keyboard = [
        [InlineKeyboardButton(await _(context, "browse_products_button"), callback_data="browse_products")],
        [InlineKeyboardButton(await _(context, "view_cart_button"), callback_data="view_cart")],
        [InlineKeyboardButton(await _(context, "my_orders_button"), callback_data="my_orders")],
        [InlineKeyboardButton(await _(context, "set_language_button"), callback_data="set_language")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = await _(context, "welcome_message", user_mention=user.mention_html())
    await update.message.reply_html(
        welcome_text,
        reply_markup=reply_markup,
    )

# --- Language Selection Handlers ---
async def set_language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query: await query.answer()

    keyboard = [
        [InlineKeyboardButton("English 🇬🇧", callback_data="lang_en")],
        [InlineKeyboardButton("Lietuvių 🇱🇹", callback_data="lang_lt")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    prompt_text = await _(context, "choose_language")

    if query:
        await query.edit_message_text(text=prompt_text, reply_markup=reply_markup)
    else: # Should be called from button, but as a fallback
        await update.message.reply_text(text=prompt_text, reply_markup=reply_markup)
    return SELECTING_LANGUAGE


async def language_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang_code = query.data.split('_')[1]  # e.g., "lang_en" -> "en"
    user_id = query.from_user.id

    context.user_data['language_code'] = lang_code
    await set_user_language_db(user_id, lang_code)

    lang_name = "English" if lang_code == "en" else "Lietuvių"
    confirmation_text = await _(context, "language_set_to", language_name=lang_name) # This will now use the new lang
    
    # Go back to main menu with new language
    user = query.effective_user
    await ensure_user_exists(user.id, user.first_name, user.username, context) # ensure context has new lang
    
    keyboard = [
        [InlineKeyboardButton(await _(context, "browse_products_button"), callback_data="browse_products")],
        [InlineKeyboardButton(await _(context, "view_cart_button"), callback_data="view_cart")],
        [InlineKeyboardButton(await _(context, "my_orders_button"), callback_data="my_orders")],
        [InlineKeyboardButton(await _(context, "set_language_button"), callback_data="set_language")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = await _(context, "welcome_message", user_mention=user.mention_html())
    
    await query.edit_message_text(
        text=f"{confirmation_text}\n\n{welcome_text}", # Show confirmation and then main menu
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    return ConversationHandler.END # End language selection, back to normal flow

# --- Modify ALL other handlers ---
# Example for browse_products:
async def browse_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    products = get_products_from_db(available_only=True)
    if not products:
        await query.edit_message_text(text=await _(context, "no_products_available"))
        return ConversationHandler.END # Or back to main menu state

    keyboard = []
    for prod_id, name, price, _ in products:
        # Product names are from DB, not translated here. Price is universal.
        keyboard.append([InlineKeyboardButton(f"{name} - {price:.2f} EUR/kg", callback_data=f"prod_{prod_id}")])
    keyboard.append([InlineKeyboardButton(await _(context, "back_to_main_menu_button"), callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text=await _(context, "products_title"), reply_markup=reply_markup)
    return SELECTING_PRODUCT

# Example for product_selected:
async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split('_')[1])
    
    product = get_product_by_id(product_id)
    if not product:
        # This needs a generic "product no longer available" or similar translated string
        await query.edit_message_text(text="Sorry, this product is no longer available.") # TODO: Translate
        return SELECTING_PRODUCT

    context.user_data['current_product_id'] = product_id
    context.user_data['current_product_name'] = product[1] # Name from DB
    context.user_data['current_product_price'] = product[2]

    prompt_text = await _(context, "product_selected_prompt", product_name=product[1])
    await query.edit_message_text(text=prompt_text)
    return TYPING_QUANTITY

# Example for quantity_typed:
async def quantity_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text
    try:
        quantity = float(user_input)
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
    except ValueError:
        await update.message.reply_text(await _(context, "invalid_quantity_prompt"))
        return TYPING_QUANTITY

    # ... (rest of the logic) ...
    product_name = context.user_data['current_product_name'] # From DB
    
    # ... (cart logic) ...
    
    await update.message.reply_text(await _(context, "item_added_to_cart", quantity=quantity, product_name=product_name))
    
    keyboard = [
        [InlineKeyboardButton(await _(context, "add_more_products_button"), callback_data="browse_products_again")],
        [InlineKeyboardButton(await _(context, "view_cart_and_checkout_button"), callback_data="view_cart")],
        [InlineKeyboardButton(await _(context, "back_to_main_menu_button"), callback_data="main_menu_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(await _(context, "what_next_prompt"), reply_markup=reply_markup)
    return SELECTING_PRODUCT

# Example for view_cart:
async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query: await query.answer()
    
    cart = context.user_data.get('cart', [])
    if not cart:
        message = await _(context, "cart_empty")
        keyboard = [[InlineKeyboardButton(await _(context, "browse_products_button"), callback_data="browse_products_again")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if query:
            await query.edit_message_text(text=message, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text=message, reply_markup=reply_markup)
        return SELECTING_PRODUCT

    cart_summary = await _(context, "your_cart_title") + "\n"
    total_price = 0
    remove_buttons = []
    for i, item in enumerate(cart):
        item_total = item['price'] * item['quantity']
        # Product name from cart (originally from DB)
        cart_summary += f"{i+1}. {item['name']} - {item['quantity']} kg x {item['price']:.2f} EUR = {item_total:.2f} EUR\n"
        total_price += item_total
        remove_buttons.append(InlineKeyboardButton(await _(context, "remove_item_button", item_index=i+1), callback_data=f"remove_{i}"))
    
    cart_summary += "\n" + await _(context, "cart_total", total_price=total_price)

    keyboard = []
    # Simple way to arrange remove buttons, max 3 per row
    for i in range(0, len(remove_buttons), 3):
        keyboard.append(remove_buttons[i:i+3])

    keyboard.extend([
        [InlineKeyboardButton(await _(context, "checkout_button"), callback_data="checkout")],
        [InlineKeyboardButton(await _(context, "add_more_products_button"), callback_data="browse_products_again")],
        [InlineKeyboardButton(await _(context, "back_to_main_menu_button"), callback_data="main_menu_action")]
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        await query.edit_message_text(text=cart_summary, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text=cart_summary, reply_markup=reply_markup)
    return SELECTING_PRODUCT

# Example for remove_item_from_cart:
async def remove_item_from_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # ... (logic to remove) ...
    # removed_item['name'] is from DB
    await query.message.reply_text(await _(context, "item_removed_from_cart", item_name=removed_item['name']))
    # ...
    return await view_cart(update, context)

# Example for checkout:
async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (cart and user logic) ...
    if order_id:
        await query.edit_message_text(
            await _(context, "order_placed_success", order_id=order_id, total_price=total_price)
        )
        # ... (admin notification - can remain in one language or be translated too if needed)
        # For simplicity, admin notification will remain in English or use a generic format
        admin_message = f"🔔 New Order #{order_id} from {user.full_name} (@{user.username}, ID: {user.id})\nTotal: {total_price:.2f} EUR\nItems:\n"
        # ...
    else:
        await query.edit_message_text(await _(context, "order_placed_error"))
    # ...
    return ConversationHandler.END

# Example for my_orders:
async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ...
    if not orders:
        message_text = await _(context, "no_orders_yet")
    else:
        message_text = await _(context, "my_orders_title") + "\n\n"
        for order_id, date, total, status, items_str in orders: # items_str is already formatted
            # Status might need translation if you store 'pending' and want to show 'Laukiama'
            # For now, let's assume status is stored in a user-friendly way or you translate it here
            status_translated = status.capitalize() # Simple capitalization, or add status keys to JSON
            message_text += await _(context, "order_details_format", 
                                    order_id=order_id, date=date, status=status_translated, 
                                    total=total, items=items_str)
    # ...
    keyboard = [[InlineKeyboardButton(await _(context, "back_to_main_menu_button"), callback_data="main_menu")]]
    # ...

# Example for back_to_main_menu:
async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (same as start, but edits message) ...
    welcome_text = await _(context, "welcome_message", user_mention=user.mention_html())
    title_text = await _(context, "main_menu_title")
    # ...
    await query.edit_message_text(
        f"{title_text}\n{welcome_text}", # Or just welcome_text, adjust as preferred
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    return ConversationHandler.END

# --- Admin Side Handlers ---
# For simplicity, the admin panel will largely remain in English or use the keys directly.
# You can apply the same translation logic to admin commands if needed by passing an admin's user_id
# to the `_` function or by having admins also set a language (less common for admin panels).
# I will translate the "You are not authorized" message.
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    # Store user_id in chat_data for `_` if effective_user is not available in deeper calls
    context.chat_data['user_id_for_translation'] = user_id 

    if user_id not in ADMIN_IDS:
        await update.message.reply_text(await _(context, "admin_unauthorized")) # Translated
        return

    # Admin panel buttons can use keys if not translated, or you can translate them
    keyboard = [
        [InlineKeyboardButton(await _(context, "admin_add_product_button"), callback_data="admin_add_prod")],
        [InlineKeyboardButton(await _(context, "admin_manage_products_button"), callback_data="admin_manage_prod")],
        [InlineKeyboardButton(await _(context, "admin_view_orders_button"), callback_data="admin_view_orders")],
        [InlineKeyboardButton(await _(context, "admin_shopping_list_button"), callback_data="admin_shopping_list")],
        [InlineKeyboardButton(await _(context, "admin_exit_button"), callback_data="admin_exit")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(await _(context, "admin_panel_title"), reply_markup=reply_markup)

# ... Continue this pattern for ALL user-facing strings in ALL handlers ...
# This includes:
# - All `reply_text`, `edit_message_text`
# - All `InlineKeyboardButton` texts
# - Any prompts or confirmation messages

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ...
    cancel_text = await _(context, "action_cancelled")
    if update.message:
        await update.message.reply_text(cancel_text, reply_markup=ReplyKeyboardRemove())
    # ...
    # (The logic to show admin panel or main menu after cancel)
    return ConversationHandler.END

# --- Main Bot Application Setup ---
def main() -> None:
    """Start the bot."""
    load_translations() # Load translations at startup
    if not translations.get("en") or not translations.get("lt"):
        logger.critical("Essential translation files (en.json, lt.json) not loaded. Aborting.")
        return
        
    init_db()

    # Ensure ADMIN_IDS is correctly parsed
    global ADMIN_IDS
    if not TELEGRAM_TOKEN or not ADMIN_TELEGRAM_ID:
        raise ValueError("TELEGRAM_TOKEN and ADMIN_TELEGRAM_ID must be set in environment variables!")
    try:
        ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_TELEGRAM_ID.split(',')]
    except ValueError:
        raise ValueError("ADMIN_TELEGRAM_ID should be a comma-separated list of numbers if multiple, or a single number.")


    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Language selection conversation handler
    language_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_language_command, pattern="^set_language$")],
        states={
            SELECTING_LANGUAGE: [
                CallbackQueryHandler(language_selected, pattern="^lang_(en|lt)$")
            ]
        },
        fallbacks=[
            CommandHandler("start", start), # Allow restarting to main menu
            CallbackQueryHandler(back_to_main_menu, pattern="^main_menu$"),
        ]
    )

    # ... (Your existing order_conv_handler, add_product_conv_handler, edit_price_conv_handler)
    # Ensure their fallbacks or entry points don't clash weirdly.
    # `start` command should always be a top-level entry.

    application.add_handler(CommandHandler("start", start))
    application.add_handler(language_conv_handler) # Add the new handler

    # ... (Add your other handlers: order_conv_handler, admin handlers, etc.)
    # Make sure they are added AFTER the language handler if there's any overlap in patterns,
    # or ensure patterns are distinct. `start` is fine as it's a command.

    # Conversation handler for user ordering process
    order_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(browse_products, pattern="^browse_products$"),
            CallbackQueryHandler(browse_products, pattern="^browse_products_again$"),
        ],
        states={
            SELECTING_PRODUCT: [
                CallbackQueryHandler(product_selected, pattern="^prod_\d+$"),
                CallbackQueryHandler(view_cart, pattern="^view_cart$"), # Moved from direct add_handler
                CallbackQueryHandler(checkout, pattern="^checkout$"),
                CallbackQueryHandler(remove_item_from_cart, pattern="^remove_\d+$"),
                CallbackQueryHandler(back_to_main_menu, pattern="^main_menu_action$"),
            ],
            TYPING_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_typed)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(back_to_main_menu, pattern="^main_menu$"),
            CommandHandler("cancel", cancel_conversation),
            CallbackQueryHandler(cancel_conversation, pattern="^cancel$")
        ],
        map_to_parent={}
    )
    application.add_handler(order_conv_handler)
    
    # Direct access handlers (if not covered by conversations or if they are entry points)
    application.add_handler(CallbackQueryHandler(view_cart, pattern="^view_cart$")) # For main menu button
    application.add_handler(CallbackQueryHandler(my_orders, pattern="^my_orders$"))
    application.add_handler(CallbackQueryHandler(back_to_main_menu, pattern="^main_menu$"))

    # Admin handlers
    # (Ensure admin_panel and other admin handlers are correctly defined and added)
    # ... (Your existing admin handlers setup) ...
    # Conversation handler for admin adding product
    add_product_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_product_start, pattern="^admin_add_prod$")],
        states={
            ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_name)],
            ADD_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_product_price)],
        },
        fallbacks=[
            CommandHandler("admin", admin_panel),
            CallbackQueryHandler(cancel_conversation, pattern="^cancel_admin_action$"), 
            CallbackQueryHandler(admin_panel_button_handler, pattern="^admin_main_panel_return$")
        ],
    )
    
    edit_price_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_change_product_price_start, pattern="^admin_change_price_\d+$")],
        states={
            EDIT_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_change_product_price_finish)],
        },
        fallbacks=[
            CommandHandler("admin", admin_panel),
            CallbackQueryHandler(admin_manage_products_button_handler, pattern="^admin_manage_prod$"), 
            CallbackQueryHandler(cancel_conversation, pattern="^cancel_admin_action$"),
        ]
    )
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(add_product_conv_handler)
    application.add_handler(edit_price_conv_handler)
    application.add_handler(CallbackQueryHandler(admin_manage_products, pattern="^admin_manage_prod$"))
    application.add_handler(CallbackQueryHandler(admin_edit_product_options, pattern="^admin_edit_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_toggle_availability, pattern="^admin_toggle_avail_\d+_\d$"))
    application.add_handler(CallbackQueryHandler(admin_delete_product_confirm, pattern="^admin_delete_confirm_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_delete_product_do, pattern="^admin_delete_do_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_view_all_orders, pattern="^admin_view_orders$"))
    application.add_handler(CallbackQueryHandler(admin_shopping_list, pattern="^admin_shopping_list$"))
    application.add_handler(CallbackQueryHandler(admin_panel_button_handler, pattern="^admin_main_panel_return$")) 
    application.add_handler(CallbackQueryHandler(admin_exit_panel, pattern="^admin_exit$"))

    logger.info("Bot starting with multi-language support...")
    application.run_polling()

if __name__ == "__main__":
    main()