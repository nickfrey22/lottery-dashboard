import time
import re
import os
import json
import smtplib
import pytz
import pandas as pd
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# --- SETTINGS ---
SCRATCHERS_URL = "https://www.calottery.com/scratchers"
DRAW_GAMES_URL = "https://www.calottery.com/draw-games"
REFRESH_URL = "https://github.com/nickfrey22/lottery-dashboard/actions/workflows/daily_schedule.yml"
CACHE_FILE = "lottery_cache.json"
CACHE_HOURS = 4  # How many hours to keep data before scraping again
HISTORY_DAYS = 14  # Number of daily snapshots to retain for trend tracking

# THRESHOLDS
HOT_THRESHOLD = 3.0
EMAIL_THRESHOLD = 3.0

# Jackpot alert thresholds (full advertised jackpot, not cash value)
JACKPOT_ALERT_THRESHOLDS = {
    "SuperLotto Plus": 6_000_000,
    "Mega Millions":  500_000_000,
    "Powerball":      500_000_000,
}

# Fixed Lower-Tier Payback Estimates
FIXED_LOWER_TIER_PAYBACK = {
    "SuperLotto Plus": 0.20,
    "Fantasy 5": 0.40,
    "Powerball": 0.18,
    "Mega Millions": 0.45 
}

# Estimated Starting CASH Jackpots
STARTING_JACKPOTS = {
    "Powerball": 10_000_000,      
    "Mega Millions": 10_000_000,  
    "SuperLotto Plus": 3_500_000, 
    "Fantasy 5": 60_000           
}

DRAW_GAME_CONFIG = {
    "Powerball":      {"price": 2.0, "odds": 292201338, "regex": r"Estimated Cash Value\s*\$([\d,]+)", "full_regex": r"Estimated jackpot\s*\$([\d,]+)"},
    "Mega Millions":  {"price": 5.0, "odds": 290472336, "regex": r"Estimated Cash Value\s*\$([\d,]+)", "full_regex": r"Estimated jackpot\s*\$([\d,]+)"},
    "SuperLotto Plus":{"price": 1.0, "odds": 41416353,  "regex": r"Estimated Cash Value\s*\$([\d,]+)", "full_regex": r"Estimated jackpot\s*\$([\d,]+)"},
    "Fantasy 5":      {"price": 1.0, "odds": 575757,    "regex": r"\$([\d,]+)\*"},
}

def clean_money(val, ticket_price=0):
    val = str(val).strip().upper()
    if 'TICKET' in val: return ticket_price
    clean_val = re.sub(r'[^\d.]', '', val)
    try: return float(clean_val)
    except: return 0.0

def parse_remaining(val):
    val = str(val).lower().replace(',', '')
    if 'of' in val:
        parts = val.split('of')
        try: return float(re.sub(r'[^\d]', '', parts[0])), float(re.sub(r'[^\d]', '', parts[1]))
        except: return 0, 0
    return 0, 0

def format_short_money(val):
    if val >= 1_000_000:
        s = val / 1_000_000
        return f"{int(s)}m" if s.is_integer() else f"{s:.1f}m"
    elif val >= 1_000:
        s = val / 1_000
        return f"{int(s)}k" if s.is_integer() else f"{s:.0f}k"
    return str(int(val))

def setup_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

# --- CACHE LOGIC ---
def load_cache():
    if not os.path.exists(CACHE_FILE):
        return None, None, None, []

    try:
        with open(CACHE_FILE, 'r') as f:
            data = json.load(f)

        # Backward compatibility: migrate legacy flat format to history array
        if 'history' not in data:
            history = [{
                'timestamp': data.get('timestamp', 0),
                'scratchers': data.get('scratchers', []),
                'draw_games': data.get('draw_games', [])
            }]
        else:
            history = data['history']

        if not history:
            return None, None, None, []

        latest = history[-1]
        timestamp = latest.get('timestamp', 0)

        if (time.time() - timestamp) < (CACHE_HOURS * 3600):
            print(f"✅ Loading data from cache ({datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')})")
            return pd.DataFrame(latest['scratchers']), pd.DataFrame(latest['draw_games']), timestamp, history
        else:
            print("⚠️ Cache expired. Scraping new data...")
            return None, None, None, history

    except Exception as e:
        print(f"Cache read error: {e}")
        return None, None, None, []

def save_cache(scratch_df, draw_df, history):
    new_entry = {
        'timestamp': time.time(),
        'scratchers': scratch_df.to_dict('records'),
        'draw_games': draw_df.to_dict('records')
    }
    history.append(new_entry)
    history = history[-HISTORY_DAYS:]
    with open(CACHE_FILE, 'w') as f:
        json.dump({'history': history}, f)
    print(f"💾 Data cached successfully. ({len(history)} snapshots stored)")
    return history

def calculate_trends(scratch_df, history):
    """Returns {game_name: 7-day CurPB% change} for each game. None if no reference data."""
    if len(history) < 2:
        return {}
    ref_index = -8 if len(history) >= 8 else 0
    ref_lookup = {g['Name']: g['CurPB'] for g in history[ref_index].get('scratchers', [])}
    trends = {}
    for _, row in scratch_df.iterrows():
        name = row['Name']
        trends[name] = row['CurPB'] - ref_lookup[name] if name in ref_lookup else None
    return trends

# --- SCRAPER FUNCTIONS ---
def get_scratcher_data(driver):
    print("Scraping Scratchers...")
    driver.get(SCRATCHERS_URL)
    time.sleep(3)
    
    try:
        tab = driver.find_element(By.XPATH, "//*[contains(text(), 'Top Prizes Remaining')]")
        tab.click()
        time.sleep(3)
    except: pass

    links = set()
    elements = driver.find_elements(By.TAG_NAME, "a")
    for elem in elements:
        href = elem.get_attribute("href")
        if href and "/scratchers/" in href and ("$" in href or href[-1].isdigit()):
            if not href.endswith("/scratchers"):
                links.add(href)
    
    game_data = []
    print(f"Found {len(links)} games. Analyzing...")
    
    for i, link in enumerate(links):
        try:
            driver.get(link)
            game_id = re.search(r'-(\d+)$', link).group(1) if re.search(r'-(\d+)$', link) else "000"
            
            try: game_name = driver.find_element(By.TAG_NAME, "h1").text.strip()
            except: game_name = "Unknown"
            
            game_name = game_name.replace("Scratchers", "").strip()
            body = driver.find_element(By.TAG_NAME, "body").text
            
            price = 0
            if "Price: $" in body:
                price = float(body.split("Price: $")[1].split()[0].strip())
            
            overall_odds = 0
            odds_match = re.search(r"Overall odds\s*:\s*1\s*in\s*([\d\.]+)", body, re.IGNORECASE)
            if odds_match: overall_odds = float(odds_match.group(1))
            
            rows = driver.find_elements(By.TAG_NAME, "tr")
            prizes = []
            for row in rows[1:]:
                cols = row.find_elements(By.TAG_NAME, "td")
                if len(cols) >= 3:
                    amt = clean_money(cols[0].text, price)
                    odds_txt = cols[1].text.replace("1 in", "").replace(",","")
                    try: odds = float(odds_txt)
                    except: odds = 0
                    rem, orig = parse_remaining(cols[2].text)
                    prizes.append({'val': amt, 'odds': odds, 'rem': rem, 'orig': orig})
            
            if not prizes: continue
            
            base_ev = 0
            for p in prizes:
                if p['odds'] > 0: base_ev += p['val'] / p['odds']
            base_payback = (base_ev / price) * 100

            valid_proxies = [p for p in prizes if p['orig'] > 0 and p['odds'] > 0]
            if not valid_proxies: continue
            proxy = sorted(valid_proxies, key=lambda x: x['odds'])[0]
            
            total_tickets = proxy['orig'] * proxy['odds']
            rem_tickets = total_tickets * (proxy['rem'] / proxy['orig'])
            
            if rem_tickets <= 0: continue
            
            curr_ev = 0
            for p in prizes:
                curr_ev += (p['rem'] * p['val']) / rem_tickets
            
            curr_payback = (curr_ev / price) * 100
            delta = curr_payback - base_payback

            sorted_prizes = sorted(prizes, key=lambda x: x['val'], reverse=True)
            top_prize = sorted_prizes[0]
            
            remain_str = f"{int(top_prize['rem'])}/{int(top_prize['orig'])}"
            top_val_str = format_short_money(top_prize['val'])
            
            game_data.append({
                'Name': f"{game_name} ({game_id})",
                'Price': price,
                'BasePB': base_payback,
                'CurPB': curr_payback,
                'Delta': delta,
                'Remain': remain_str,
                'TopPrize': top_val_str,
                'OverallOdds': overall_odds
            })
            
        except Exception as e:
            continue
            
    return pd.DataFrame(game_data).sort_values('CurPB', ascending=False)

def get_draw_data(driver):
    print("Scraping Draw Games...")
    driver.get(DRAW_GAMES_URL)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(3)
    
    text = driver.find_element(By.TAG_NAME, "body").text
    marker = "Game Card"
    indices = [m.start() for m in re.finditer(marker, text)]
    indices.append(len(text))
    
    results = []
    
    for i in range(len(indices) - 1):
        block = text[indices[i]-50 : indices[i+1]]
        for name, cfg in DRAW_GAME_CONFIG.items():
            if name.upper() in text[indices[i]-50 : indices[i]].upper():
                match = re.search(cfg['regex'], block)
                if match:
                    jackpot = clean_money(match.group(1))
                else:
                    if name == "Fantasy 5":
                        bk = re.search(r"\$([\d,]+)", text[indices[i]:indices[i+1]])
                        jackpot = clean_money(bk.group(1)) if bk else 0
                    else: jackpot = 0
                
                # Scrape full (annuity) jackpot if regex defined
                full_jackpot = 0
                if 'full_regex' in cfg:
                    full_match = re.search(cfg['full_regex'], block, re.IGNORECASE)
                    if full_match:
                        full_jackpot = clean_money(full_match.group(1))

                if jackpot > 1000:
                    curr_ev = (jackpot / cfg['odds']) + (cfg['price'] * FIXED_LOWER_TIER_PAYBACK.get(name, 0.2))
                    curr_pb = (curr_ev / cfg['price']) * 100

                    start_jackpot = STARTING_JACKPOTS.get(name, 0)
                    base_ev = (start_jackpot / cfg['odds']) + (cfg['price'] * FIXED_LOWER_TIER_PAYBACK.get(name, 0.2))
                    base_pb = (base_ev / cfg['price']) * 100

                    results.append({
                        'Name': name,
                        'Jackpot': jackpot,
                        'FullJackpot': full_jackpot,
                        'Price': cfg['price'],
                        'CurPB': curr_pb,
                        'BasePB': base_pb
                    })
    
    return pd.DataFrame(results).sort_values('CurPB', ascending=False)

# --- EMAIL ---
def send_alert_email(hot_games):
    email_user = os.environ.get('EMAIL_USER')
    email_pass = os.environ.get('EMAIL_PASS')
    
    if not email_user or not email_pass:
        print("Skipping email: No credentials found.")
        return

    msg = MIMEMultipart()
    msg['From'] = email_user
    msg['To'] = email_user
    msg['Subject'] = f"🚨 LOTTERY ALERT: {len(hot_games)} Games Found!"

    body = "The following games have a Delta of +3.0% or higher:\n\n"
    for _, row in hot_games.iterrows():
        body += f"GAME: {row['Name']}\n"
        body += f"PRICE: ${row['Price']:.0f}\n"
        body += f"PAYBACK: {row['CurPB']:.1f}% (Delta: +{row['Delta']:.1f}%)\n"
        body += f"TOP PRIZE: {row['TopPrize']} ({row['Remain']} left)\n"
        body += "-" * 30 + "\n"
    
    body += f"\nCheck Dashboard: https://nickfrey22.github.io/lottery-dashboard/"
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(email_user, email_pass)
        server.send_message(msg)
        server.quit()
        print("✅ Alert email sent successfully.")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

def send_jackpot_alert_email(triggered_games):
    email_user = os.environ.get('EMAIL_USER')
    email_pass = os.environ.get('EMAIL_PASS')

    if not email_user or not email_pass:
        print("Skipping jackpot email: No credentials found.")
        return

    msg = MIMEMultipart()
    msg['From'] = email_user
    msg['To'] = email_user
    msg['Subject'] = f"🎰 JACKPOT ALERT: {len(triggered_games)} game(s) hit threshold!"

    body = "The following jackpots have reached your alert threshold:\n\n"
    for _, row in triggered_games.iterrows():
        threshold = JACKPOT_ALERT_THRESHOLDS.get(row['Name'], 0)
        full = row.get('FullJackpot', 0)
        body += f"GAME:      {row['Name']}\n"
        body += f"JACKPOT:   ${full:,.0f}\n"
        body += f"THRESHOLD: ${threshold:,.0f}\n"
        body += f"CASH VALUE: ${row['Jackpot']:,.0f}\n"
        body += "-" * 30 + "\n"

    body += f"\nCheck Dashboard: https://nickfrey22.github.io/lottery-dashboard/"
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(email_user, email_pass)
        server.send_message(msg)
        server.quit()
        print("✅ Jackpot alert email sent successfully.")
    except Exception as e:
        print(f"❌ Failed to send jackpot email: {e}")

# --- HTML GENERATOR ---
def calculate_buy_score(df):
    """Composite 0-100 buy score: 50% CurPB, 30% Delta, 20% top-prize remaining ratio."""
    result = df.copy()

    def parse_top_ratio(remain_str):
        try:
            rem, orig = remain_str.split('/')
            return float(rem) / float(orig)
        except:
            return 0.5

    result['_TopRatio'] = result['Remain'].apply(parse_top_ratio)

    def norm(series, lo, hi):
        return ((series - lo) / (hi - lo) * 100).clip(0, 100)

    result['BuyScore'] = (
        0.50 * norm(result['CurPB'], 50, 95) +
        0.30 * norm(result['Delta'], -10, 15) +
        0.20 * (result['_TopRatio'] * 100)
    ).round(0).astype(int)

    return result.drop(columns=['_TopRatio']).sort_values('BuyScore', ascending=False)

def generate_html(scratchers, draw_games, timestamp, trends):
    tz = pytz.timezone('America/Los_Angeles')
    dt_object = datetime.fromtimestamp(timestamp, tz)
    time_str = dt_object.strftime('%m/%d %I:%M %p')

    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Lottery Dashboard</title>
        <meta name="robots" content="noindex, nofollow">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 0 auto; padding: 10px; background: #f4f4f9; }
            h1 { text-align: center; color: #333; font-size: 1.5em; }
            .btn-refresh { 
                display: block; width: 200px; margin: 0 auto 20px auto; 
                padding: 10px; background-color: #007bff; color: white; 
                text-align: center; text-decoration: none; border-radius: 5px; font-weight: bold;
            }
            .card { background: white; padding: 10px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 20px; overflow-x: auto; }
            table { width: 100%; border-collapse: collapse; font-size: 12px; }
            th, td { padding: 6px 2px; text-align: center; border-bottom: 1px solid #ddd; }
            td:first-child { text-align: left; } 
            th:not(:first-child), td:not(:first-child) { width: 1%; white-space: nowrap; }
            th { background-color: #007bff; color: white; vertical-align: bottom; font-size: 11px; }
            tr:nth-child(even) { background-color: #f9f9f9; }
            .hot-row { background-color: #e6fffa !important; }
            .hot-val { color: green; font-weight: bold; }
            .timestamp { text-align: center; color: #666; font-size: 0.8em; margin-bottom: 20px; }
        </style>
    </head>
    <body>
        <h1>🎱Lottery Tracker</h1>
        <p class="timestamp">Updated: TIME_PLACEHOLDER PST</p>
        <a href="REFRESH_URL_PLACEHOLDER" target="_blank" class="btn-refresh">🔄 Force Refresh</a>

        <div class="card">
            <h2>🏆 Draw Games</h2>
            <table>
                <tr>
                    <th style="text-align:left">Game</th>
                    <th>Jackpot<br>(Cash)</th>
                    <th>Base<br>PB%</th>
                    <th>Cur.<br>PB%</th>
                </tr>
                DRAW_ROWS_PLACEHOLDER
            </table>
        </div>
        
        <div class="card">
            <h2>🔄 Best Churn Games (Low Risk)</h2>
            <p style="text-align:center; font-size: 0.8em; color: #666;">Games under $10 sorted by Best Odds to win ANY prize.</p>
            <table>
                <tr>
                    <th style="text-align:left">Game</th>
                    <th>$</th>
                    <th>Odds<br>(1 in)</th>
                    <th>Cur.<br>PB%</th>
                </tr>
                CHURN_ROWS_PLACEHOLDER
            </table>
        </div>

        <div class="card">
            <h2>🔥 Scratchers</h2>
            <table>
                <tr>
                    <th style="text-align:left">Game</th>
                    <th>Score</th>
                    <th>$</th>
                    <th>Top</th>
                    <th>Rem.</th>
                    <th>Base<br>PB%</th>
                    <th>Cur.<br>PB%</th>
                    <th>Delta</th>
                    <th>7d<br>Trend</th>
                </tr>
                SCRATCHER_ROWS_PLACEHOLDER
            </table>
        </div>
    </body>
    </html>
    """
    
    draw_rows = ""
    for _, r in draw_games.iterrows():
        draw_rows += f"<tr><td style='text-align:left'>{r['Name']}</td><td>${format_short_money(r['Jackpot'])}</td><td>{r['BasePB']:.0f}%</td><td class='hot-val'>{r['CurPB']:.0f}%</td></tr>"

    churn_df = scratchers[scratchers['Price'] <= 10].sort_values('OverallOdds', ascending=True).head(5)
    churn_rows = ""
    for _, r in churn_df.iterrows():
        churn_rows += f"<tr><td style='text-align:left'>{r['Name']}</td><td>${int(r['Price'])}</td><td>{r['OverallOdds']:.2f}</td><td>{r['CurPB']:.0f}%</td></tr>"

    scored_scratchers = calculate_buy_score(scratchers)
    scratcher_rows = generate_scratcher_rows(scored_scratchers, trends)

    final_html = html_template.replace("TIME_PLACEHOLDER", time_str)
    final_html = final_html.replace("REFRESH_URL_PLACEHOLDER", REFRESH_URL)
    final_html = final_html.replace("DRAW_ROWS_PLACEHOLDER", draw_rows)
    final_html = final_html.replace("CHURN_ROWS_PLACEHOLDER", churn_rows)
    final_html = final_html.replace("SCRATCHER_ROWS_PLACEHOLDER", scratcher_rows)

    with open("index.html", "w", encoding='utf-8') as f:
        f.write(final_html)

def generate_scratcher_rows(df, trends):
    rows = ""
    for _, r in df.head(20).iterrows():
        is_hot = r['Delta'] >= HOT_THRESHOLD
        row_class = "class='hot-row'" if is_hot else ""
        delta_color = "green" if r['Delta'] > 0 else "red"
        delta_str = f"{r['Delta']:+.1f}"
        
        trend_val = trends.get(r['Name']) if trends else None
        if trend_val is None:
            trend_td = "<td style='color:#aaa;'>—</td>"
        else:
            trend_color = "green" if trend_val > 0 else "red"
            trend_td = f"<td style='color:{trend_color}; font-weight:bold;'>{trend_val:+.1f}</td>"

        score = r.get('BuyScore', 0)
        if score >= 70:
            score_td = f"<td style='color:green; font-weight:bold;'>{score}</td>"
        elif score >= 50:
            score_td = f"<td style='color:darkorange; font-weight:bold;'>{score}</td>"
        else:
            score_td = f"<td style='color:#999;'>{score}</td>"

        rows += f"""
        <tr {row_class}>
            <td style='text-align:left; max-width: 120px;'>{r['Name']}</td>
            {score_td}
            <td>{int(r['Price'])}</td>
            <td>{r['TopPrize']}</td>
            <td>{r['Remain']}</td>
            <td>{r['BasePB']:.1f}%</td>
            <td>{r['CurPB']:.1f}%</td>
            <td style='color:{delta_color}; font-weight:bold;'>{delta_str}</td>
            {trend_td}
        </tr>
        """
    return rows

def main():
    # 1. Try to load cache
    scratch_df, draw_df, timestamp, history = load_cache()
    
    # 2. If no cache, scrape
    if scratch_df is None:
        driver = setup_driver()
        try:
            draw_df = get_draw_data(driver)
            scratch_df = get_scratcher_data(driver)
            history = save_cache(scratch_df, draw_df, history)
            timestamp = time.time()
        finally:
            driver.quit()
    
    # 3. Calculate trends and generate HTML
    trends = calculate_trends(scratch_df, history)
    generate_html(scratch_df, draw_df, timestamp, trends)
    print("Dashboard generated.")
    
    # 4. Email Alerts
    hot_games = scratch_df[
        (scratch_df['Delta'] >= EMAIL_THRESHOLD) &
        (scratch_df['Price'] >= 20.0)
    ]
    if not hot_games.empty:
        print(f"Found {len(hot_games)} hot games. Sending email...")
        send_alert_email(hot_games)

    # Jackpot threshold alerts
    if 'FullJackpot' in draw_df.columns:
        jackpot_hits = draw_df[
            draw_df.apply(
                lambda r: r['FullJackpot'] >= JACKPOT_ALERT_THRESHOLDS.get(r['Name'], float('inf')),
                axis=1
            )
        ]
        if not jackpot_hits.empty:
            print(f"Found {len(jackpot_hits)} jackpot threshold hit(s). Sending email...")
            send_jackpot_alert_email(jackpot_hits)

if __name__ == "__main__":
    main()
