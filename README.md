# Kapila – WhatsApp Enquiry Bot

A minimal FastAPI webhook server for the **Meta WhatsApp Cloud API**.  
It receives incoming WhatsApp messages and replies with rule-based responses.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure constants

Open `main.py` and replace the placeholder values:

| Constant          | Description                                      |
|-------------------|--------------------------------------------------|
| `VERIFY_TOKEN`    | Any secret string you set in the Meta dashboard  |
| `ACCESS_TOKEN`    | Permanent or temporary token from Meta           |
| `PHONE_NUMBER_ID` | Your WhatsApp Business phone number ID           |

### 3. Run the server

```bash
uvicorn main:app --reload --port 8000
```

### 4. Expose with ngrok (for local development)

```bash
ngrok http 8000
```

Copy the HTTPS URL and paste it in the Meta App dashboard as your webhook URL:

```
https://<your-ngrok-id>.ngrok-free.app/webhook
```

Set the **Verify Token** to match `VERIFY_TOKEN` in `main.py`.

## Endpoints

| Method | Path       | Purpose                       |
|--------|------------|-------------------------------|
| GET    | `/webhook` | Webhook verification by Meta  |
| POST   | `/webhook` | Receives incoming messages    |

## Reply Keywords

| Keyword              | Response                  |
|----------------------|---------------------------|
| `hi` / `hello`       | Welcome message           |
| `price` / `cost`     | Room pricing details      |
| `room` / `available` | Room availability info    |
| _anything else_      | Default help message      |
