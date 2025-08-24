import logging
import fitz  # PyMuPDF
from openai import AsyncOpenAI
import os
import json
import datetime
import asyncio
import sqlite3
import httpx
from googlesearch import search
from serpapi import GoogleSearch
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler, CallbackQueryHandler, PicklePersistence
)
from keep_alive import keep_alive

# ---------- Load Environment Variables ----------
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")

# ---------- AI Client Setup ----------
client = AsyncOpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

# ---------- Logging ----------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ---------- Constants ----------
GET_LANGUAGE, GET_TITLE, GET_AUTHOR, WAITING_PDF, AWAIT_QUOTE_APPROVAL, EDIT_PROMPT, EDIT_QUOTES, CONFIRM_ACTION, WAIT_SCHEDULE = range(9)
DB_FILE = "queue.db"
CHANNEL_ID = os.getenv("CHANNEL_ID")

# ---------- Database Setup ----------
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                title TEXT,
                author TEXT,
                quotes TEXT,
                mode TEXT,
                posted INTEGER DEFAULT 0,
                scheduled_time TEXT
            )
        """)

# ---------- Helpers ----------
def add_to_queue(book):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO queue (chat_id, title, author, quotes, mode, scheduled_time)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            book['chat_id'],
            book['title'],
            book['author'],
            json.dumps(book['quotes'], ensure_ascii=False),
            book['mode'],
            book['scheduled_time']
        ))

def get_queue():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute("SELECT id, title, author, posted, mode FROM queue")
        return cursor.fetchall()

def mark_posted(book_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE queue SET posted = 1 WHERE id = ?", (book_id,))

def delete_from_queue(index):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM queue WHERE id = ?", (index,))

# ---------- Text Extraction ----------
def extract_text_from_pdf(file_path):
    text = ""
    with fitz.open(file_path) as doc:
        for page in doc:
            text += page.get_text()
    return text

# ---------- AI Quote Extraction ----------
async def extract_quotes_from_text(prompt):
    try:
        response = await client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Error from Groq API: {e}")
        return f"❌ Error from Groq API: {e}"

# ---------- Start & Help Commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add a Book", callback_data="addbook_start")],
        [InlineKeyboardButton("რი Queue", callback_data="queue_show"), InlineKeyboardButton("❓ Help", callback_data="help_show")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome! I'm your personal Book Quote Bot. 📚\n\n"
        "I can find a book online, extract memorable quotes, and post them to your channel.\n\n"
        "To get started, use the /addbook command or press the button below.",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Here are the available commands:\n\n"
        "📖 *Book Management*\n"
        "/addbook - Start the process of adding a new book.\n"
        "/cancel - Cancel the current operation at any time.\n\n"
        "🗂️ *Queue Management*\n"
        "/queue - Show the list of books waiting to be posted.\n"
        "/remove `[id]` - Remove a specific book from the queue.\n"
        "/postnow `[id]` - Post a book from the queue immediately."
    )
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(text, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, parse_mode='Markdown')

# ---------- Add Book Conversation ----------
async def get_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    lang = query.data.split('_')[1]
    context.user_data['book_info'] = {'language': lang}
    
    await query.edit_message_text(f"✅ Language set to {lang.capitalize()}. Now, what is the book's title?")
    
    return GET_TITLE

async def addbook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear() # Start fresh
    context.user_data['book_info'] = {}
    
    keyboard = [
        [InlineKeyboardButton("🇮🇷 Persian", callback_data="lang_persian"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang_english")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📚 First, please select the language of the book:",
        reply_markup=reply_markup
    )
    return GET_LANGUAGE

async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['book_info']["title"] = update.message.text
    await update.message.reply_text(
        "Now send me the author's name:",
        reply_markup=ReplyKeyboardRemove()
    )
    return GET_AUTHOR

async def get_author(update: Update, context: ContextTypes.DEFAULT_TYPE):
    book_info = context.user_data.get('book_info', {})
    book_info["author"] = update.message.text
    language = book_info.get('language')

    await update.message.reply_text(
        f"✅ Got it: '{book_info['title']}' by {book_info['author']}.",
        reply_markup=ReplyKeyboardRemove(),
    )

    if language == 'english':
        await update.message.reply_text("Searching online for the book...")
        query = f'"{book_info["title"]}" "{book_info["author"]}" filetype:pdf'
        logging.info(f"Starting book search for query: {query}")
        try:
            search_params = {
                "api_key": SERPER_API_KEY,
                "q": query,
                "engine": "google",
            }
            async with httpx.AsyncClient() as client:
                response = await client.post('https://google.serper.dev/search', json=search_params, timeout=20.0)
                response.raise_for_status()
                search_results = response.json()
            
            pdf_url = next((result.get('link') for result in search_results.get('organic', []) if result.get('link', '').endswith('.pdf')), None)

            if pdf_url:
                await update.message.reply_text(f"Found a PDF online! Downloading from {pdf_url}...")
                file_path = f"{update.effective_chat.id}_downloaded_book.pdf"
                async with httpx.AsyncClient(timeout=45.0) as client:
                    pdf_response = await client.get(pdf_url, follow_redirects=True)
                    pdf_response.raise_for_status()
                    with open(file_path, "wb") as f:
                        f.write(pdf_response.content)
                return await process_book_and_get_quotes(update, context, file_path)
            else:
                await update.message.reply_text(
                    "I couldn't find a PDF online. Please upload the book manually.",
                )
                return WAITING_PDF
        except Exception as e:
            logging.error(f"Error during book search or download for '{book_info['title']}': {e}", exc_info=True)
            await update.message.reply_text(
                "An error occurred while searching. Please upload the PDF manually.",
            )
            return WAITING_PDF
    
    elif language == 'persian':
        return await generate_persian_reflection(update, context)

    return ConversationHandler.END

# ---------- Persian Book Reflection Flow ----------
async def generate_persian_reflection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    book_info = context.user_data.get('book_info', {})
    title = book_info.get('title')
    author = book_info.get('author')

    await context.bot.send_message(
        chat_id=chat_id,
        text="🤖 Generating a reflection for this Persian book...\nThis involves searching for summaries and reviews online, so it may take a moment."
    )

    prompt = (
        f"You are a literary analyst specializing in Persian literature. For the book '{title}' by '{author}', "
        "please perform the following tasks:\n"
        "1. Search the internet for high-quality summaries and reviews from trusted Persian sources (like Wikipedia, literary blogs, or news articles).\n"
        "2. Browse user reviews on Goodreads for this book, translating them to English if necessary to understand the general sentiment.\n"
        "3. Based on your findings, write a short, original, and insightful reflection or comment about the book in PERSIAN. This should not be a summary, but a thoughtful take on its themes, impact, or legacy.\n"
        "Return ONLY the final Persian reflection, without any of your own commentary or explanation."
    )
    context.user_data['prompt'] = prompt

    reflection_text = await extract_quotes_from_text(prompt)

    book = {
        "chat_id": chat_id,
        "title": title,
        "author": author,
        "quotes": [reflection_text],  # Store reflection in the quotes field
        "mode": 'persian_reflection',
        "scheduled_time": None
    }
    context.user_data['pending_book'] = book

    preview = f"📘 *{book['title']}*\n✍️ _{book['author']}_\n\nReflection:\n{reflection_text}"

    keyboard = [
        [InlineKeyboardButton("✅ Approve", callback_data="approve_quotes")],
        [InlineKeyboardButton("🔄 Regenerate", callback_data="retry_extraction")],
        [InlineKeyboardButton("❌ Reject", callback_data="reject_quotes")]
    ]

    await context.bot.send_message(
        chat_id=chat_id,
        text=preview,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return AWAIT_QUOTE_APPROVAL

# ---------- Quote Generation and Approval Flow ----------
async def rerun_quote_extraction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    message = update.message
    
    await query.edit_message_text("🔄 Rerunning quote extraction with the same prompt...")

    prompt = context.user_data['prompt']
    quotes_raw = await extract_quotes_from_text(prompt)
    quotes = [q.strip() for q in quotes_raw.split('\n') if q.strip()]
    context.user_data['pending_book']['quotes'] = quotes

    preview = f"📘 *{context.user_data['pending_book']['title']}*\n✍️ _{context.user_data['pending_book']['author']}_\n\n"
    preview += '\n'.join([f"🔹 {q}" for q in quotes])

    keyboard = [
        [InlineKeyboardButton("✅ Approve", callback_data="approve_quotes")],
        [InlineKeyboardButton("✏️ Edit Quotes", callback_data="edit_quotes")],
        [InlineKeyboardButton("❌ Reject", callback_data="reject_quotes")]
    ]
    await query.edit_message_text(
        preview,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AWAIT_QUOTE_APPROVAL

async def quote_approval_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'approve_quotes':
        keyboard = [
            [InlineKeyboardButton("✅ Confirm & Add to Queue", callback_data="confirm_add")],
            [InlineKeyboardButton("⏰ Schedule Post", callback_data="schedule")],
            [InlineKeyboardButton("🚀 Post Now", callback_data="post_now")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        await query.edit_message_text(
            text="✅ Quotes approved. What would you like to do next?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CONFIRM_ACTION

    elif data == 'edit_quotes':
        book = context.user_data.get('pending_book')
        if not book or not book.get('quotes'):
            await query.edit_message_text("Could not find quotes to edit. Please try again.")
            return ConversationHandler.END
        
        current_quotes_text = '\n'.join(book['quotes'])
        
        await query.edit_message_text(
            "You can copy the text below, edit it, and send it back.\n\n"
            f"```{current_quotes_text}```",
            parse_mode='Markdown'
        )
        return EDIT_QUOTES

    elif data == 'edit_prompt':
        await query.edit_message_text(
            "Please send me the new prompt to use for extracting quotes."
        )
        return EDIT_PROMPT

    elif data == 'reject_quotes':
        keyboard = [
            [InlineKeyboardButton("🔁 Retry with Same Prompt", callback_data="retry_extraction")],
            [InlineKeyboardButton("✏️ Edit Prompt", callback_data="edit_prompt")],
            [InlineKeyboardButton("🛑 Cancel", callback_data="cancel")]
        ]
        await query.edit_message_text(
            text="Quote generation rejected. What would you like to do?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAIT_QUOTE_APPROVAL
    
    elif data == 'retry_extraction':
        return await rerun_quote_extraction(update, context)

async def receive_new_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_prompt = update.message.text
    book = context.user_data.get('pending_book', {})

    # For English books, combine the new prompt with the book's text
    if book.get('mode') == 'english_quotes' and 'book_text' in context.user_data:
        book_text = context.user_data['book_text']
        full_prompt = f"{new_prompt}\n\nText:\n{book_text[:5000]}"
    # For Persian books, the prompt is the full instruction
    else:
        full_prompt = new_prompt

    context.user_data['prompt'] = full_prompt
    
    await update.message.reply_text("✅ Got it. Generating new quotes with your prompt...")
    
    # Fake an update object for rerun
    class FakeQuery:
        async def edit_message_text(self, *args, **kwargs):
            return await context.bot.send_message(chat_id=update.effective_chat.id, *args, **kwargs)
    class FakeUpdate:
        def __init__(self, message):
            self.callback_query = FakeQuery()
            self.message = message

    return await rerun_quote_extraction(FakeUpdate(update.message), context)

async def receive_edited_quotes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    edited_text = update.message.text
    new_quotes = [q.strip() for q in edited_text.split('\n') if q.strip()]
    
    book = context.user_data['pending_book']
    book['quotes'] = new_quotes
    
    await update.message.reply_text("✅ Quotes updated. Here is the new preview:")

    preview = f"📘 *{book['title']}*\n✍️ _{book['author']}_\n\n"
    preview += '\n'.join([f"🔹 {q}" for q in new_quotes])

    keyboard = [
        [InlineKeyboardButton("✅ Approve", callback_data="approve_quotes")],
        [InlineKeyboardButton("✏️ Edit Quotes", callback_data="edit_quotes")],
        [InlineKeyboardButton("❌ Reject", callback_data="reject_quotes")]
    ]

    await update.message.reply_text(
        preview,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    return AWAIT_QUOTE_APPROVAL

# ---------- Manual PDF Handling ----------
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        file = await update.message.document.get_file()
        file_path = f"{chat_id}_uploaded_book.pdf"
        await file.download_to_drive(file_path)
        
        return await process_book_and_get_quotes(update, context, file_path)
    except Exception as e:
        logging.error(f"Error handling uploaded PDF: {e}")
        await update.message.reply_text("Sorry, I had trouble processing that file. Please try again or /cancel.")
        return WAITING_PDF

# ---------- Confirmation and Scheduling ----------
async def post_book(context: ContextTypes.DEFAULT_TYPE, book: dict):
    try:
        preview = f"📘 *{book['title']}*\n✍️ _{book['author']}_\n\n"
        if book['mode'] == 'persian_reflection':
            preview += f"**Reflection:**\n{book['quotes'][0]}"
        else:
            preview += '\n'.join([f"🔹 {q}" for q in book['quotes']])
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=preview,
            parse_mode='Markdown'
        )
        return True
    except Exception as e:
        logging.error(f"Error posting book {book.get('id')}: {e}")
        return False

async def receive_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Simple parsing: expects 'YYYY-MM-DD HH:MM'
        time_str = update.message.text
        scheduled_time = datetime.datetime.strptime(time_str, '%Y-%m-%d %H:%M')
        
        book = context.user_data.get('pending_book')
        book['scheduled_time'] = scheduled_time.isoformat()
        add_to_queue(book)

        await update.message.reply_text(
            f"✅ Book scheduled for {scheduled_time.strftime('%Y-%m-%d at %H:%M')}."
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text(
            "Invalid format. Please use YYYY-MM-DD HH:MM. Or /cancel."
        )
        return WAIT_SCHEDULE

async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Please send the scheduled time in `YYYY-MM-DD HH:MM` format (24-hour clock).",
        parse_mode='Markdown'
    )
    return WAIT_SCHEDULE

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    book = context.user_data.get('pending_book')

    if not book:
        await query.edit_message_text("Something went wrong. Please start over with /addbook.")
        return ConversationHandler.END

    if data == 'confirm_add':
        add_to_queue(book)
        await query.edit_message_text("✅ Book added to the queue!")
        return ConversationHandler.END

    elif data == 'schedule':
        await query.edit_message_text("⏰ When should I schedule this post?")
        return await schedule_post(update, context)

    elif data == 'post_now':
        await query.edit_message_text("🚀 Posting to the channel now...")
        success = await post_book(context, book)
        if success:
            await query.edit_message_text("✅ Successfully posted to the channel!")
        else:
            await query.edit_message_text("❌ Failed to post. The book has been added to the queue for a manual retry.")
            add_to_queue(book) # Add to queue if posting fails
        return ConversationHandler.END

    elif data == 'cancel':
        await query.edit_message_text("Operation cancelled.")
        return ConversationHandler.END

# ---------- Process Book and Get Quotes ----------
async def process_book_and_get_quotes(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str):
    chat_id = update.effective_chat.id
    try:
        await update.message.reply_text("✅ File received. Extracting text...", reply_markup=ReplyKeyboardRemove())
        text = extract_text_from_pdf(file_path)
        context.user_data['book_text'] = text

        prompt = (
            "You are a literary expert. From the following English book text, extract "
            "exactly 3 meaningful, popular, and thought-provoking quotes. "
            "Each quote should be a complete sentence that captures a key theme or moment. "
            "Return only the quotes, each on a new line, without any explanation or quotation marks.\n\n"
            f"Text:\n{text[:8000]}"
        )
        context.user_data['prompt'] = prompt

        await update.message.reply_text("🤖 Extracting quotes with AI. This might take a moment...")
        quotes_raw = await extract_quotes_from_text(prompt)
        quotes = [q.strip() for q in quotes_raw.split('\n') if q.strip()]

        book_info = context.user_data.get('book_info')
        if not book_info or 'title' not in book_info or 'author' not in book_info:
            await update.message.reply_text("Something went wrong, my apologies. Please start over with /addbook.")
            return ConversationHandler.END

        book = {
            "chat_id": chat_id,
            "title": book_info["title"],
            "author": book_info["author"],
            "quotes": quotes,
            "mode": 'english_quotes',
            "scheduled_time": None
        }
        context.user_data['pending_book'] = book

        preview = f"📘 *{book['title']}*\n✍️ _{book['author']}_\n\n"
        preview += '\n'.join([f"🔹 {q}" for q in quotes])

        keyboard = [
            [InlineKeyboardButton("✅ Approve", callback_data="approve_quotes")],
            [InlineKeyboardButton("✏️ Edit Quotes", callback_data="edit_quotes")],
            [InlineKeyboardButton("❌ Reject", callback_data="reject_quotes")]
        ]

        await update.message.reply_text(
            preview,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        return AWAIT_QUOTE_APPROVAL

    except Exception as e:
        await update.message.reply_text(f"❌ Error processing file: {e}")
        return ConversationHandler.END
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# ---------- Handle PDF Document ----------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    file = await update.message.document.get_file()
    file_path = f"{update.message.document.file_unique_id}.pdf"
    await file.download_to_drive(file_path)

    file_path = f"{update.message.document.file_unique_id}.pdf"
    await file.download_to_drive(file_path)
    return await process_book_and_get_quotes(update, context, file_path)

# ---------- Callback Query Handler ----------
async def confirm_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    book = context.user_data.get('pending_book')

    if not book:
        await query.edit_message_text("No book found in session.")
        return ConversationHandler.END

    if data == "confirm_add":
        book["scheduled_time"] = datetime.datetime.now().isoformat()
        add_to_queue(book)
        await query.edit_message_text("✅ Book saved to queue.")
        return ConversationHandler.END

    elif data == "schedule":
        await query.edit_message_text("Please send the schedule time in format YYYY-MM-DD HH:MM (24h).")
        return WAIT_SCHEDULE

    elif data == "post_now":
        message = f"📘 *{book['title']}*\n✍️ _{book['author']}_\n\n"
        message += '\n'.join([f"🔹 {q}" for q in book['quotes']])
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='Markdown')
            await query.edit_message_text("✅ Posted immediately to the channel.")
        except telegram.error.TimedOut:
            await query.edit_message_text("❌ Posting failed due to a timeout. Please try again.")
        except Exception as e:
            await query.edit_message_text(f"❌ Posting error: {e}")
        return ConversationHandler.END

# ---------- Receive Schedule Time ----------
async def receive_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text
    try:
        scheduled_time = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        if scheduled_time < datetime.datetime.now():
            await update.message.reply_text("❌ The scheduled time must be in the future. Please try again.")
            return WAIT_SCHEDULE
        context.user_data['pending_book']['scheduled_time'] = scheduled_time.isoformat()
        add_to_queue(context.user_data['pending_book'])
        await update.message.reply_text(f"✅ Book scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M')}.")
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Invalid format. Please send time like YYYY-MM-DD HH:MM")
        return WAIT_SCHEDULE

# ---------- Cancel Handler ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation, cleaning up user data."""
    query = update.callback_query
    message = update.message

    context.user_data.clear()

    cancel_message = "Action canceled. What would you like me to do next?"
    
    if query:
        await query.answer()
        await query.edit_message_text(text="Action canceled.")
    elif message:
        await message.reply_text(
            "Action canceled.",
            reply_markup=ReplyKeyboardRemove()
        )
    
    # Follow up with the main menu
    await start(update, context)

    return ConversationHandler.END

# ---------- Queue Management Commands ----------
async def show_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_queue()
    if not rows:
        await update.message.reply_text("📭 Queue is empty.")
        return

    msg = "📚 Current Queue:\n"
    for row in rows:
        id, title, author, posted, mode = row
        status = '✅' if posted else '⏳'
        msg += f"\n{id}. {title} by {author} — {status} ({mode})"
    await update.message.reply_text(msg)

async def remove_from_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /remove [id]")
        return

    try:
        book_id_to_remove = int(context.args[0])
        delete_from_queue(book_id_to_remove)
        await update.message.reply_text(f"❌ Removed book with ID {book_id_to_remove} from queue.")
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Invalid ID. Please provide a valid number from the /queue list.")

# ---------- Post Now Command ----------
async def post_book_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /postnow [id]")
        return

    try:
        book_id = int(context.args[0])
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.execute("SELECT title, author, quotes, mode FROM queue WHERE id = ?", (book_id,))
            row = cursor.fetchone()
        
        if not row:
            await update.message.reply_text("❌ Book not found.")
            return

        title, author, quotes_json, mode = row
        quotes = json.loads(quotes_json)
        
        book_data = {
            'title': title,
            'author': author,
            'quotes': quotes,
            'mode': mode
        }

        success = await post_book(context, book_data)
        if success:
            mark_posted(book_id)
            await update.message.reply_text("✅ Book posted now!")
        else:
            await update.message.reply_text("❌ Failed to post the book. Please check the logs.")

    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Invalid ID. Please provide a valid number from the /queue list.")

# ---------- Auto Posting Loop ----------
async def auto_post_loop(app):
    while True:
        now = datetime.datetime.now()
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.execute(
                "SELECT id, title, author, quotes, mode FROM queue WHERE posted = 0 AND scheduled_time IS NOT NULL"
            )
            for row in cursor.fetchall():
                book_id, title, author, quotes_json, mode = row
                scheduled_time = datetime.datetime.fromisoformat(conn.execute("SELECT scheduled_time FROM queue WHERE id = ?", (book_id,)).fetchone()[0])
                
                if now >= scheduled_time:
                    quotes = json.loads(quotes_json)
                    book_data = {
                        'title': title,
                        'author': author,
                        'quotes': quotes,
                        'mode': mode
                    }
                    logging.info(f"Auto-posting scheduled book ID: {book_id}")
                    success = await post_book(app, book_data)
                    if success:
                        mark_posted(book_id)
                    else:
                        logging.error(f"Auto-posting failed for book ID: {book_id}")

        await asyncio.sleep(60)

# ---------- Main ----------
import nest_asyncio

async def main():
    init_db()

    # ---------- Bot Setup ----------
    persistence = PicklePersistence(filepath="bot_persistence.pickle")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    add_book_conv = ConversationHandler(
        entry_points=[CommandHandler('addbook', addbook), CallbackQueryHandler(addbook, pattern='^addbook_start$')],
        states={
            GET_LANGUAGE: [CallbackQueryHandler(get_language, pattern='^lang_')],
            GET_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            GET_AUTHOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_author)],
            WAITING_PDF: [MessageHandler(filters.ATTACHMENT, handle_pdf)],
            AWAIT_QUOTE_APPROVAL: [
                CallbackQueryHandler(quote_approval_flow, pattern='^(approve_quotes|edit_quotes|reject_quotes|retry_extraction|edit_prompt)$'),
            ],
            EDIT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_prompt)],
            EDIT_QUOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edited_quotes)],
            CONFIRM_ACTION: [CallbackQueryHandler(handle_confirmation)],
            WAIT_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule_time)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^cancel$")
        ],
        per_user=True,
        per_chat=True,
        name="add_book_conversation"
    )

    application.add_handler(add_book_conv)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("queue", show_queue))
    application.add_handler(CallbackQueryHandler(show_queue, pattern='^queue_show$'))
    application.add_handler(CommandHandler("remove", remove_from_queue, block=False))
    application.add_handler(CommandHandler("postnow", post_book_now, block=False))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(help_command, pattern='^help_show$'))

    print("Bot is running...")
    asyncio.create_task(auto_post_loop(application))
    await application.run_polling()

def start_http_server(port=5000):
    """Start a simple HTTP server to satisfy Railway's requirements"""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'Telegram bot is running!')

    server = HTTPServer(('0.0.0.0', port), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    return server

if __name__ == '__main__':
    try:
        # Start the keep-alive server
        keep_alive()
        
        # Initialize the bot
        nest_asyncio.apply()
        
        # Run the bot
        print("Starting bot...")
        asyncio.get_event_loop().run_until_complete(main())
        
    except Exception as e:
        print(f"Error starting bot: {e}")
        import traceback
        traceback.print_exc()
        raise