import time
import re
import pandas as pd
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# --- SETTINGS ---
SCRATCHERS_URL = "https://www.calottery.com/scratchers"
DRAW_GAMES_URL = "https://www.calottery.com/draw-games"

# YOUR GITHUB ACTIONS LINK (Safe to expose)
REFRESH_URL = "https://github.com/nickfrey22/lottery-dashboard/actions/workflows/daily_schedule.yml"

# Fixed Lower-Tier Payback Estimates (Draw Games)
FIXED_LOWER_TIER_PAYBACK = {
    "SuperLotto Plus": 0.20,
    "Fantasy 5": 0.40,
    "Powerball": 0.18,
    "Mega Millions": 0.45 
}

DRAW_GAME_CONFIG = {
    "Powerball": {"price": 2.0, "odds": 292201338, "regex": r"Estimated Cash Value\s*\$([\d,]+)"},
    "Mega Millions": {"price": 5.0, "odds": 290472336, "regex": r"Estimated Cash Value\s*\$([\d,]+)"},
    "SuperLotto Plus": {"price": 1.0, "odds": 41416353, "regex": r"Estimated Cash Value\s*\$([\d,]+)"},
    "Fantasy 5": {"price": 1.0, "odds": 575757, "regex": r"\$([\d,]+)\*"}
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

def setup_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

# --- SCRATCHER LOGIC ---
def get_scratcher_data(driver):
    print("Scraping Scratchers (This takes a few minutes)...")
    driver.get(SCRATCHERS_URL)
    time.sleep(3)
    
    try:
        tab = driver.find_element(By.XPATH, "//*[contains(text(), 'Top Prizes Remaining')]")
        tab.click()
        time.sleep(3)
    except: print("Could not click tab, trying to proceed...")

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
            
            body = driver.find_element(By.TAG_NAME, "body").text
            price = 0
            if "Price: $" in body:
                price = float(body.split("Price: $")[1].split()[0].strip())
            
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
            
            # 1. EV Calc
            valid_proxies = [p for p in prizes if p['orig'] > 0 and p['odds'] > 0]
            if not valid_proxies: continue
            proxy = sorted(valid_proxies, key=lambda x: x['odds'])[0]
            
            total_tickets = proxy['orig'] * proxy['odds']
            rem_tickets = total_tickets * (proxy['rem'] / proxy['orig'])
            
            if rem_tickets <= 0: continue
            
            curr_ev = 0
            for p in prizes:
                curr_ev += (p['rem'] * p['val']) / rem_tickets
            
            payback = (curr_ev / price) * 100

            # 2. Get Top Prize Stats
            sorted_prizes = sorted(prizes, key=lambda x: x['val'], reverse=True)
            top_prize = sorted_prizes[0]
            top_prize_str = f"{int(top_prize['rem'])} of {int(top_prize['orig'])}"
            top_prize_val = f"${top_prize['val']:,.0f}"
            
            game_data.append({
                'Name': f"{game_name} ({game_id})",
                'Price': price,
                'EV': curr_ev,
                'Payback': payback,
                'JackpotStatus': top_prize_str,
                'TopPrize': top_prize_val
            })
            
        except Exception as e:
            continue
            
    return pd.DataFrame(game_data).sort_values('Payback', ascending=False)

# --- DRAW GAME LOGIC ---
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
                
                if jackpot > 1000:
                    ev = (jackpot / cfg['odds']) + (cfg['price'] * FIXED_LOWER_TIER_PAYBACK.get(name, 0.2))
                    payback = (ev / cfg['price']) * 100
                    results.append({'Name': name, 'Jackpot': jackpot, 'Price': cfg['price'], 'Payback': payback})
    
    return pd.DataFrame(results).sort_values('Payback', ascending=False)

# --- HTML GENERATOR ---
def generate_html(scratchers, draw_games):
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Lottery Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f4f4f9; }}
            h1 {{ text-align: center; color: #333; }}
            .btn-refresh {{ 
                display: block; width: 200px; margin: 0 auto 20px auto; 
                padding: 10px; background-color: #007bff; color: white; 
                text-align: center; text-decoration: none; border-radius: 5px; font-weight: bold;
            }}
            .btn-refresh:hover {{ background-color: #0056b3; }}
            .card {{ background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 20px; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
            th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }}
            th {{ background-color: #007bff; color: white; }}
            tr:nth-child(even) {{ background-color: #f9f9f9; }}
            .hot {{ color: green; font-weight: bold; }}
            .timestamp {{ text-align: center; color: #666; font-size: 0.8em; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <h1>🎱 Nick's Lottery Tracker</h1>
        <p class="timestamp">Last Updated: {datetime.now().strftime('%Y-%m-%d %I:%M %p PST')}</p>
        
        <a href="{REFRESH_URL}" target="_blank" class="btn-refresh">🔄 Force Refresh</a>

        <div class="card">
            <h2>🏆 Best Draw Games</h2>
            <table>
                <tr><th>Game</th><th>Jackpot (Cash)</th><th>Payback %</th></tr>
                {''.join(f"<tr><td>{r['Name']}</td><td>${r['Jackpot']:,.0f}</td><td class='hot'>{r['Payback']:.1f}%</td></tr>" for _, r in draw_games.iterrows())}
            </table>
        </div>

        <div class="card">
            <h2>🔥 Top 10 Hot Scratchers</h2>
            <table>
                <tr><th>Game</th><th>Price</th><th>Top Prize</th><th>Jackpots Left</th><th>Payback %</th></tr>
                {''.join(f"<tr><td>{r['Name']}</td><td>${r['Price']:.0f}</td><td>{r['TopPrize']}</td><td>{r['JackpotStatus']}</td><td class='hot'>{r['Payback']:.1f}%</td></tr>" for _, r in scratchers.head(10).iterrows())}
            </table>
        </div>
    </body>
    </html>
    """
    with open("index.html", "w", encoding='utf-8') as f:
        f.write(html)

def main():
    driver = setup_driver()
    try:
        draw_df = get_draw_data(driver)
        scratch_df = get_scratcher_data(driver)
        generate_html(scratch_df, draw_df)
        print("Dashboard generated successfully.")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
