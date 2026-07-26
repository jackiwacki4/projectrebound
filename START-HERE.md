# Start here — running the sports bot on your Mac

Written for someone who has never used a terminal. Every step is one thing to
copy-paste or one thing to click. Do them in order.

**First, three things that should make this less scary:**

- The bot **cannot move your money**. Not a design choice you have to trust — any
  web request that looks like a deposit, withdrawal, or transfer is blocked in
  code and the process fails on the spot. Adding or removing money is something
  only you can do, by hand, in Kalshi's own app.
- The bot **does not place any bets** until you edit a file to say so. It is off
  by default. Right now all it does is watch prices and write down what it would
  have done.
- Nothing here can break your Mac. If a step fails, it fails harmlessly and you
  can just run it again.

You will need: a Mac, and a Kalshi account.

---

## Step 1 — Open Terminal

Press `Cmd` + `Space`, type `terminal`, press `Enter`. A window with text appears.
That's where everything below gets pasted. Paste with `Cmd` + `V`, then press
`Enter` to run it.

## Step 2 — Download the code and set it up

Copy this whole line, paste it into Terminal, press `Enter`:

```sh
cd ~ && git clone https://github.com/jackiwacki4/projectrebound.git && cd projectrebound && git checkout claude/sports-bot-qs28l9 && cd kalshi-bot && ./setup.sh
```

This takes about two minutes. It downloads the code and installs what it needs.
When it finishes it prints a short list of next steps — those are Steps 3 to 5
below.

> **If it says `destination path 'projectrebound' already exists`:** you already
> downloaded it. Run this instead:
> ```sh
> cd ~/projectrebound && git checkout claude/sports-bot-qs28l9 && git pull && cd kalshi-bot && ./setup.sh
> ```

> **If it says `xcode-select` or asks to install developer tools:** click Install,
> wait for it to finish, then run the line from Step 2 again.

## Step 3 — Create your Kalshi API key

This part is in your web browser, not the terminal.

1. Go to **kalshi.com** and sign in.
2. Click your **account** → **Profile** → **API Keys**.
3. Create a new key.
4. Kalshi shows you **two** things. You need both:
   - a **Key ID** — a long string of letters, numbers and dashes. Copy it
     somewhere you can find in a minute (a Notes window is fine).
   - a **private key file** — Kalshi gives you this **once and never again**.
     Download it, or if it shows it as text on screen, select all of it and copy.

## Step 4 — Put the private key file where the bot expects it

In Terminal:

```sh
cd ~/projectrebound/kalshi-bot && open secrets
```

A Finder window opens. Now:

- **If Kalshi gave you a file to download:** drag it from your Downloads folder
  into this window, then rename it to exactly `kalshi_private_key.pem`.
- **If Kalshi showed the key as text you copied:** run this instead, paste the
  text into the window that opens, then press `Cmd` + `S` to save and close it:
  ```sh
  touch secrets/kalshi_private_key.pem && open -e secrets/kalshi_private_key.pem
  ```

The name must be exactly `kalshi_private_key.pem`. Not `.pem.txt`, not
`kalshi-key.pem`.

## Step 5 — Tell the bot your Key ID

Paste this line. It will ask you for the Key ID from Step 3 — paste that in and
press `Enter`:

```sh
cd ~/projectrebound/kalshi-bot; printf 'Paste your Kalshi Key ID, then press Enter: '; read KEYID; printf 'KALSHI_API_KEY_ID=%s\nKALSHI_PRIVATE_KEY_PATH=./secrets/kalshi_private_key.pem\nKALSHI_ENVIRONMENT=prod\n' "$KEYID" > .env; echo "--- saved, here is the result ---"; cat .env
```

It prints back what it saved. You should see your Key ID after
`KALSHI_API_KEY_ID=` and the word `prod` at the bottom. If the Key ID line is
empty, run the line again.

## Step 6 — Start it

```sh
cd ~/projectrebound/kalshi-bot && ./run.sh run --config config/sports.yaml
```

It will print lines as it works. **Leave this window open** — closing it stops
the bot. That's it; it's running.

If it refuses to start it will tell you in plain English what is missing (a
wrong key path, a missing Key ID) rather than a wall of red text. Fix that one
thing and run the line again.

## Step 7 — Check it's actually working

Open a **second** Terminal window (`Cmd` + `N`) and paste:

```sh
cd ~/projectrebound/kalshi-bot && ./run.sh report --config config/sports.yaml
```

Look for these three lines near the top:

```
  data collected : 1234 book snapshots (56 with live quotes)
  sports inputs  : 126 ratings from 3 methods, 15 game states, 42 games linked
  >> WORKING. ...
```

- **`with live quotes`** must be more than 0. That means real prices are arriving.
- **`games linked`** should be roughly half your market count (each game has two
  markets — one per team).
- **`ratings from 3 methods`** — fewer than 3 in the first hour is fine, one
  method needs some game history before it will speak up.

The scoring sections stay empty until games finish and markets settle. That's
normal — it has nothing to score yet.

---

## How to stop it

- **Stop the bot entirely:** click the first Terminal window and press
  `Control` + `C`.
- **Stop only real bets, keep collecting data** (you won't need this until you
  turn real betting on):
  ```sh
  cd ~/projectrebound/kalshi-bot && touch HALT
  ```
  Delete that file to allow them again: `rm HALT`.

## Reading the results later

Run the Step 7 command any time. Don't draw conclusions early: the report tells
you when the sample is too small. Judge it by how many **markets have settled**,
not by how many lines it printed.

## If something goes wrong

| What you see | What to do |
|---|---|
| `command not found: git` | A popup should offer to install developer tools. Click Install, wait, retry Step 2. |
| `Can't find your private key file` | Step 4 — the file name must be exactly `kalshi_private_key.pem`. |
| `Your Kalshi Key ID isn't set yet` | Step 5 — run that line again and check it prints your Key ID. |
| `CERTIFICATE_VERIFY_FAILED` | Run `./setup.sh` again; it installs the fix. |
| Warnings about one provider failing | Normal and safe. That source is skipped for that cycle. |
| Every provider failing | Your internet or a firewall, not the bot. |

## What NOT to do yet

Do not set `live_trading: enabled: true`. That is what makes it place real
orders with real money. There is a checklist in `kalshi-bot/README.md` under
"Before you EVER enable live trading" — it exists for good reasons, and none of
it matters until you have weeks of collected data saying there is an edge worth
chasing.
