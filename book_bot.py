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
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler, CallbackQueryHandler, PicklePersistence
)

# ---------- Load Environment Variables ----------
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

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
GET_TITLE, GET_AUTHOR, WAITING_PDF, AWAIT_QUOTE_APPROVAL, EDIT_PROMPT, EDIT_QUOTES, CONFIRM_ACTION, WAIT_SCHEDULE = range(8)
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

# ---------- Global User State ----------
user_state = {}

# ---------- Start & Help Commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📜 Help & Commands", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome! I'm your personal Book Quote Bot. 📚\n\n"
        "I can find a book online, extract memorable quotes, and post them to your channel.\n\n"
        "To get started, use the /addbook command.",
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
async def addbook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_state[chat_id] = {}
    await update.message.reply_text(
        "Let's add a new book. What is the title?",
        reply_markup=ReplyKeyboardRemove()
    )
    return GET_TITLE

async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_chat.id]["title"] = update.message.text
    reply_keyboard = [["Cancel"]]
    await update.message.reply_text(
        "Now send me the author's name:",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return GET_AUTHOR

async def get_author(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_info = user_state.get(chat_id, {})
    user_info["author"] = update.message.text

    await update.message.reply_text(
        f"✅ Got it: '{user_info['title']}' by {user_info['author']}.\n\nSearching online for the book...",
        reply_markup=ReplyKeyboardRemove(),
    )

    query = f'"{user_info["title"]}" "{user_info["author"]}" filetype:pdf'
    logging.info(f"Starting book search for query: {query}")
    try:
        # Run synchronous search in a separate thread to avoid blocking
        search_generator = search(query, num=5)
        logging.info("Search generator created. Converting to list in thread.")
        search_results = await asyncio.to_thread(list, search_generator)
        logging.info(f"Search completed. Found {len(search_results)} results.")
        pdf_url = next((url for url in search_results if url.endswith(".pdf")), None)
        logging.info(f"PDF URL found: {pdf_url}")

        if pdf_url:
            await update.message.reply_text(f"Found a PDF online! Downloading from {pdf_url}...")
            file_path = f"{chat_id}_downloaded_book.pdf"
            
            # Use httpx for async download
            async with httpx.AsyncClient() as client:
                response = await client.get(pdf_url, follow_redirects=True, timeout=30.0)
                response.raise_for_status()

                with open(file_path, "wb") as f:
                    f.write(response.content)

            return await process_book_and_get_quotes(update, context, file_path)

        else:
            reply_keyboard = [["Cancel"]]
            await update.message.reply_text(
                "I couldn't find a PDF online. Please upload the book manually.",
                reply_markup=ReplyKeyboardMarkup(
                    reply_keyboard, one_time_keyboard=True, resize_keyboard=True
                ),
            )
            return WAITING_PDF

    except Exception as e:
        logging.error(f"Error during book search or download: {e}")
        reply_keyboard = [["Cancel"]]
        await update.message.reply_text(
            "An error occurred while searching. Please upload the PDF manually.",
            reply_markup=ReplyKeyboardMarkup(
                reply_keyboard, one_time_keyboard=True, resize_keyboard=True
            ),
        )
        return WAITING_PDF

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
    new_prompt_template = update.message.text
    book_text = context.user_data['book_text']
    
    full_prompt = f"{new_prompt_template}\n\nText:\n{book_text[:5000]}"
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


# ---------- Process Book and Get Quotes ----------
async def process_book_and_get_quotes(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str):
    chat_id = update.effective_chat.id
    try:
        await update.message.reply_text("✅ File received. Extracting text...", reply_markup=ReplyKeyboardRemove())
        text = extract_text_from_pdf(file_path)
        context.user_data['book_text'] = text

        prompt = (
            "You are a literary expert. From the following Persian book text, extract "
            "exactly 3 famous, meaningful, and impactful quotes or sentences. "
            "Each quote should be complete, expressive, and ideally a sentence that "
            "readers often remember or quote. Return only the quotes without explanation.\n\n"
            f"Text:\n{text[:5000]}"
        )
        context.user_data['prompt'] = prompt

        await update.message.reply_text("🤖 Extracting quotes with AI. This might take a moment...")
        quotes_raw = await extract_quotes_from_text(prompt)
        quotes = [q.strip() for q in quotes_raw.split('\n') if q.strip()]

        user = user_state.get(chat_id)
        if not user:
            await update.message.reply_text("Please start with /addbook first.")
            return ConversationHandler.END

        book = {
            "chat_id": chat_id,
            "title": user["title"],
            "author": user["author"],
            "quotes": quotes,
            "mode": user.get("mode", "auto"),
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
    """Cancels and ends the conversation."""
    query = update.callback_query
    message = update.message

    # Clean up state
    chat_id = update.effective_chat.id
    if chat_id in user_state:
        del user_state[chat_id]
    if 'pending_book' in context.user_data:
        del context.user_data['pending_book']

    cancel_message = "Action canceled. What would you like me to do next?"
    if query:
        await query.answer()
        await query.edit_message_text(text=cancel_message)
    elif message:
        await message.reply_text(
            cancel_message,
            reply_markup=ReplyKeyboardRemove(),
        )

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
    try:
        index = int(context.args[0])
        delete_from_queue(index)
        await update.message.reply_text(f"❌ Removed book with ID {index} from queue.")
    except:
        await update.message.reply_text("⚠️ Usage: /remove [id]")

# ---------- Post Now Command ----------
async def post_book_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        book_id = int(context.args[0])
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.execute("SELECT title, author, quotes FROM queue WHERE id = ?", (book_id,))
            row = cursor.fetchone()
            if not row:
                await update.message.reply_text("❌ Book not found.")
                return
            title, author, quotes_json = row
            quotes = json.loads(quotes_json)                                                             
            message = f"📘 *{title}*\n✍️ _{author}_\n\n"
            message += '\n'.join([f"🔹 {q}" for q in quotes])

            await context.bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='Markdown')
            mark_posted(book_id)
            await update.message.reply_text("✅ Book posted now!")
    except:
        await update.message.reply_text("⚠️ Usage: /postnow [id]")

# ---------- Auto Posting Loop ----------
async def auto_post_loop(app):
    while True:
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.execute(
                "SELECT id, title, author, quotes FROM queue WHERE mode = 'auto' AND posted = 0 AND scheduled_time <= ?", (now,))
            for row in cursor.fetchall():
                book_id, title, author, quotes_json = row
                quotes = json.loads(quotes_json)

                message = f"📘 *{title}*\n✍️ _{author}_\n\n"
                message += '\n'.join([f"🔹 {q}" for q in quotes])
                try:
                    await app.bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='Markdown')
                    mark_posted(book_id)
                except telegram.error.TimedOut:
                    logging.warning(f"Timeout error when posting book {book_id}. Retrying next cycle.")
                except Exception as e:
                    logging.error(f"Error posting book {book_id}: {e}")
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

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addbook", addbook)],
        states={
            GET_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            GET_AUTHOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_author)],
            WAITING_PDF: [MessageHandler(filters.Document.PDF, handle_document)],
            AWAIT_QUOTE_APPROVAL: [CallbackQueryHandler(quote_approval_flow, pattern=r"^(approve_quotes|edit_quotes|edit_prompt|reject_quotes|retry_extraction|cancel)$")],
            EDIT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_prompt)],
            EDIT_QUOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edited_quotes)],
            CONFIRM_ACTION: [CallbackQueryHandler(confirm_action_handler, pattern=r"^(confirm_add|schedule|post_now|cancel)$")],
            WAIT_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(r'(?i)^cancel$'), cancel),
            CallbackQueryHandler(cancel, pattern=r"^cancel$")
        ],
        per_message=False,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("queue", show_queue))
    application.add_handler(CommandHandler("remove", remove_from_queue))
    application.add_handler(CommandHandler("postnow", post_book_now))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(help_command, pattern=r"^help$"))

    print("Bot is running...")
    asyncio.create_task(auto_post_loop(application))
    await application.run_polling()

if __name__ == '__main__':
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main())