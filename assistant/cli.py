"""The interface adapter — a REPL. Everything Cash-specific lives in agent.py/extraction.py/
api_client.py; this file only knows how to read a line, hand it to TransactionAgent, and print
the reply. Swapping this for WhatsApp later means writing a new adapter with this same shape
(receive text + a sender id, call agent.handle_message(sender_id, text), send the reply back) —
nothing in the other three files changes.
"""
import os
import sys

from dotenv import load_dotenv
from langchain_ollama import ChatOllama

from agent import TransactionAgent
from api_client import CashApiClient, CashApiError

# A WhatsApp adapter would use the sender's phone number here instead — one process serving many
# senders, each with their own row in SessionStore. A CLI process only ever talks to one person,
# so a fixed id is enough.
SESSION_ID = 'cli'

REQUIRED_ENV_VARS = ('CASH_API_URL', 'CASH_USERNAME', 'CASH_PASSWORD')


def main():
    load_dotenv()

    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        sys.exit(f'Falta configurar {", ".join(missing)} en assistant/.env '
                  f'(copia .env.example y completa tus datos).')

    # num_predict caps how many tokens the model may generate. Without it, a small model that
    # falls into a repetition loop (observed live: mistral:latest ran for almost 2 hours filling
    # and re-filling its context window, never emitting a stop token, for what should have been
    # a ~40-token JSON reply) has nothing to stop it. The expected output is a short JSON object
    # — every successful call so far generated well under 50 tokens — so 256 is generous headroom
    # that still turns a runaway generation into a fast, visible failure instead of a silent hang.
    llm = ChatOllama(model=os.environ.get('OLLAMA_MODEL', 'mistral:latest'), temperature=0, num_predict=256)
    api = CashApiClient(
        base_url=os.environ['CASH_API_URL'],
        username=os.environ['CASH_USERNAME'],
        password=os.environ['CASH_PASSWORD'],
    )
    agent = TransactionAgent(llm, api)

    print('Cash assistant — escribe una transacción (ej. "14000 a checho"), o "salir" para terminar.')
    while True:
        try:
            text = input('> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in ('salir', 'exit', 'quit'):
            break

        try:
            reply = agent.handle_message(SESSION_ID, text)
        except CashApiError as exc:
            print(f'Error hablando con la API de Cash: {exc}')
        except Exception as exc:
            # The LLM boundary is genuinely unreliable (see extraction.py's json_mode note) — a
            # single malformed reply shouldn't kill the whole chat session.
            print(f'No pude procesar eso, ¿puedes reformularlo? ({exc})')
        else:
            print(reply)


if __name__ == '__main__':
    main()
