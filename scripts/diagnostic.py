#!/usr/bin/env python3
"""Cora diagnostic sweep — tests every major layer and reports pass/fail."""
import os, sqlite3, subprocess, sys
sys.path.insert(0, 'src')
from dotenv import load_dotenv
load_dotenv('.env', override=True)

results = []
def ok(label, detail=''):   results.append(('OK',   label, detail))
def warn(label, detail=''):  results.append(('WARN', label, detail))
def fail(label, detail=''):  results.append(('FAIL', label, detail))

# ── 1. Channel routing ───────────────────────────────────────────────────────
from cora.entity_router import route, is_silent_channel
CHANNEL_TESTS = [
    ('hjrg-leadership','FNDR'), ('hjrg-finance','FNDR'), ('fndr','FNDR'),
    ('f3e-leadership','F3E'), ('f3-sales','F3E'), ('f3-events','F3E'),
    ('f3-sponsorships','F3E'), ('polar-metrics','F3E'),
    ('lex-leadership','LEX'), ('llc-leadership','LEX-LLC'),
    ('lts-leadership','LEX-LTS'), ('lbhs-leadership','LEX-LBHS'),
    ('lla-leadership','LEX-LLA'), ('shaun-leadership','LEX'),
    ('osn-leadership','OSN'), ('osngm-leadership','OSNGM'),
    ('osngf-leadership','OSNGF'), ('osngw-leadership','OSNGW'),
    ('osnvv-leadership','OSNVV'),
    ('bdm-leadership','BDM'), ('media','BDM'),
    ('hjrp-leadership','HJRP'), ('hjrp-1337','HJRP-1337'),
    ('hjrp-1555','HJRP-1555'), ('rogers-ranch','HJRP'),
    ('ufl-leadership','UFL'), ('f3c','F3C'),
    ('hjrprod','HJRPROD'), ('harrisonjrogers-social','HJRPROD'),
]
SILENT = ['asana-feed','fireflies-recaps','notion-changes','hubspot-feed','drive-shares']
routing_fails = []
for ch, expected in CHANNEL_TESTS:
    got = route(ch)
    if got != expected:
        routing_fails.append(ch + ': got ' + got + ' expected ' + expected)
for ch in SILENT:
    if not is_silent_channel(ch):
        routing_fails.append(ch + ': should be silent')
if routing_fails:
    fail('Channel routing', ' | '.join(routing_fails))
else:
    ok('Channel routing', str(len(CHANNEL_TESTS)) + ' channels + ' + str(len(SILENT)) + ' silent all correct')

# ── 2. User authorization ────────────────────────────────────────────────────
from cora import user_access
AUTH_TESTS = [
    ('U0B2RM2JYJ1','F3E',True,'Harrison->F3E'),
    ('U0B2RM2JYJ1','LEX',True,'Harrison->LEX'),
    ('U0B2RM2JYJ1','OSN',True,'Harrison->OSN'),
    ('U0B3VGWJTMJ','F3E',True,'Alex->F3E'),
    ('U0B3VGWJTMJ','OSN',False,'Alex->OSN blocked'),
    ('U0B3VGWJTMJ','UFL',True,'Alex->UFL'),
    ('U0B3RU5Q55G','F3E',True,'Tommy->F3E'),
    ('U0B3RU5Q55G','OSN',False,'Tommy->OSN blocked'),
    ('U0B3RU5Q55G','LEX',False,'Tommy->LEX blocked'),
    ('U0B3PS7RFJA','OSN',True,'Matt->OSN'),
    ('U0B3PS7RFJA','F3E',False,'Matt->F3E blocked'),
    ('U0B3PS82G30','LEX-LLC',True,'Shaun->LEX-LLC'),
    ('U0B3PS82G30','F3E',False,'Shaun->F3E blocked'),
    ('U0B3VGT8RE0','LEX-LLC',True,'Jen->LEX-LLC'),
    ('U0B3KHBJJ91','LEX-LLC',True,'Jeff->LEX-LLC'),
    ('U0B3RU65TFU','BDM',True,'Demi->BDM'),
    ('U0B3RU65TFU','F3E',False,'Demi->F3E blocked'),
    ('U0B3NGR1Y85','BDM',True,'Larry->BDM'),
    ('U0B3NGR1Y85','F3E',True,'Larry->F3E'),
    ('U0B3AEQS0NB','F3E',True,'Hannah->F3E'),
    ('U0B3AEQS0NB','LEX',True,'Hannah->LEX'),
    ('U0B4L78SZHN','OSN',True,'Micah->OSN'),
    ('U0B4L78SZHN','BDM',True,'Micah->BDM'),
]
auth_fails = []
for uid, entity, expected, label in AUTH_TESTS:
    got = user_access.is_authorized(uid, entity)
    if got != expected:
        auth_fails.append(label + ': got ' + str(got))
if auth_fails:
    fail('User authorization', ' | '.join(auth_fails))
else:
    ok('User authorization', str(len(AUTH_TESTS)) + ' user/entity checks all correct')

# ── 3. Sensitive topic blocking ──────────────────────────────────────────────
TOPIC_TESTS = [
    ('U0B3RU5Q55G','F3E','what is the F3E p&l this month',True,'Tommy->financials blocked'),
    ('U0B3RU5Q55G','F3E','what is the F3E p and l this month',True,'Tommy->p and l blocked'),
    ('U0B3PS82G30','LEX-LLC','client diagnosis for room 3',True,'Shaun->PHI blocked'),
    ('U0B3RU65TFU','BDM','BDM cap table ownership',False,'Demi->cap table allowed (full access)'),
    ('U0B3VGWJTMJ','F3E','F3E revenue this quarter',True,'Alex->financials blocked'),
    ('U0B2RM2JYJ1','F3E','F3E p&l',False,'Harrison->financials allowed'),
    ('U0B3AEQS0NB','F3E','what is the F3E cash position',False,'Hannah->cash position allowed (ops anchor)'),
    ('U0B3AEQS0NB','F3E','what is F3E cash flow',False,'Hannah->cash flow allowed (ops anchor)'),
    ('U0B3AEQS0NB','F3E','what is the F3E cap table equity',True,'Hannah->cap_table blocked'),
    ('U0B3NGR1Y85','BDM','who is the BDM owner equity split',True,'Larry->cap_table blocked'),
    ('U0B3NGR1Y85','BDM','what is the BDM p&l',True,'Larry->financials blocked'),
]
topic_fails = []
for uid, entity, msg, should_block, label in TOPIC_TESTS:
    block = user_access.check_access(uid, entity, msg)
    blocked = block is not None
    if blocked != should_block:
        topic_fails.append(label + ': expected blocked=' + str(should_block) + ' got ' + str(blocked))
if topic_fails:
    fail('Sensitive topic blocking', ' | '.join(topic_fails))
else:
    ok('Sensitive topic blocking', str(len(TOPIC_TESTS)) + ' topic/user checks all correct')

# ── 4. System prompts ────────────────────────────────────────────────────────
from cora.prompt_loader import load_prompt
ENTITIES = ['F3E','LEX','LEX-LLC','LEX-LTS','LEX-LBHS','LEX-LLA',
            'OSN','OSNGM','OSNVV','OSNGF','OSNGW','BDM','FNDR',
            'HJRP','UFL','F3C','HJRPROD']
prompt_fails = []
for e in ENTITIES:
    try:
        p = load_prompt(e)
        if len(p) < 200:
            prompt_fails.append(e + ': too short (' + str(len(p)) + ' chars)')
    except Exception as ex:
        prompt_fails.append(e + ': ' + str(ex)[:60])
if prompt_fails:
    fail('System prompts', ' | '.join(prompt_fails))
else:
    ok('System prompts', 'All ' + str(len(ENTITIES)) + ' entity prompts loaded successfully')

# ── 5. KB coverage ───────────────────────────────────────────────────────────
conn = sqlite3.connect('data/cora_kb.db')
rows = conn.execute('SELECT entity, COUNT(*) FROM knowledge_chunks GROUP BY entity ORDER BY 2 DESC').fetchall()
total = conn.execute('SELECT COUNT(*) FROM knowledge_chunks').fetchone()[0]
conn.close()
chunk_map = {r[0]: r[1] for r in rows}
thin = [e for e in ['F3E','LEX','OSN','BDM','FNDR','HJRP'] if chunk_map.get(e,0) < 50]
ok('KB total', str(total) + ' chunks across ' + str(len(rows)) + ' entities')
if thin:
    warn('KB thin entities', 'Under 50 chunks: ' + ', '.join(thin))
else:
    ok('KB entity coverage', 'All major entities have adequate chunks')

# ── 6. API keys & tool clients ───────────────────────────────────────────────
try:
    from cora.tools import asana_client
    asana_client._pat()
    ok('Asana PAT', 'Present')
except Exception as e:
    fail('Asana PAT', str(e)[:80])

try:
    from cora.tools import hubspot_client
    hubspot_client._token()
    ok('HubSpot token', 'Present')
except Exception as e:
    fail('HubSpot token', str(e)[:80])

try:
    from cora.connectors import notion_connector
    notion_connector._api_key()
    ok('Notion API key', 'Present')
except Exception as e:
    fail('Notion API key', str(e)[:80])

try:
    from cora.connectors import qbo_oauth
    ents = qbo_oauth.list_provisioned_entities()
    ok('QBO tokens', str(len(ents)) + ' entities: ' + ', '.join(sorted(ents)))
except Exception as e:
    fail('QBO tokens', str(e)[:80])

key = os.environ.get('ANTHROPIC_API_KEY','')
if key.startswith('sk-ant-'):
    ok('Anthropic API key', 'Present')
else:
    fail('Anthropic API key', 'Missing or wrong format')

key = os.environ.get('OPENAI_API_KEY','')
if key.startswith('sk-'):
    ok('OpenAI API key', 'Present')
else:
    fail('OpenAI API key', 'Missing')

for var, label in [('SLACK_BOT_TOKEN','Slack bot token'),
                    ('FIREFLIES_API_KEY','Fireflies API key'),
                    ('HUBSPOT_PRIVATE_APP_TOKEN','HubSpot token'),
                    ('GOOGLE_SERVICE_ACCOUNT_JSON','Google SA JSON path')]:
    val = os.environ.get(var,'')
    if val:
        ok(label, 'Present')
    else:
        fail(label, 'Missing from .env')

# ── 7. Scheduled tasks ───────────────────────────────────────────────────────
result = subprocess.run(['schtasks','/Query','/FO','CSV','/NH'],
                        capture_output=True, text=True, timeout=15)
expected_disabled = {'cowork-cora-qbo-token-refresh','cowork-cora-asana-email-sync'}
task_fails = []
task_count = 0
for line in result.stdout.splitlines():
    parts = line.strip().strip('"').split('","')
    if len(parts) < 3: continue
    name = parts[0].lstrip('\\')
    status = parts[2]
    if not (name.startswith('cowork-cora') or name.startswith('Cora')): continue
    task_count += 1
    if name in expected_disabled:
        if 'Disabled' not in status:
            task_fails.append(name + ' should be Disabled')
    elif name == 'cowork-cora-service':
        if 'Running' not in status:
            task_fails.append(name + ' should be Running, is ' + status)
    else:
        if 'Ready' not in status and 'Running' not in status:
            task_fails.append(name + ' unexpected: ' + status)
if task_fails:
    fail('Scheduled tasks', ' | '.join(task_fails))
else:
    ok('Scheduled tasks', str(task_count) + ' tasks all in expected states')

# ── 8. Live API pings ────────────────────────────────────────────────────────
import httpx
token = os.environ.get('SLACK_BOT_TOKEN','')
try:
    r = httpx.get('https://slack.com/api/auth.test',
                  headers={'Authorization': 'Bearer ' + token}, timeout=8)
    d = r.json()
    if d.get('ok'): ok('Slack API live', 'Connected as ' + d.get('user','?'))
    else: fail('Slack API live', d.get('error','?'))
except Exception as e:
    fail('Slack API live', str(e)[:60])

hs_tok = os.environ.get('HUBSPOT_PRIVATE_APP_TOKEN','')
try:
    r = httpx.get('https://api.hubapi.com/crm/v3/owners',
                  headers={'Authorization': 'Bearer ' + hs_tok}, timeout=8)
    if r.status_code == 200: ok('HubSpot API live', str(len(r.json().get('results',[]))) + ' owners')
    else: fail('HubSpot API live', 'HTTP ' + str(r.status_code))
except Exception as e:
    fail('HubSpot API live', str(e)[:60])

notion_key = os.environ.get('NOTION_API_KEY','')
try:
    r = httpx.get('https://api.notion.com/v1/users/me',
                  headers={'Authorization': 'Bearer ' + notion_key,
                           'Notion-Version': '2022-06-28'}, timeout=8)
    if r.status_code == 200: ok('Notion API live', 'Connected')
    else: fail('Notion API live', 'HTTP ' + str(r.status_code))
except Exception as e:
    fail('Notion API live', str(e)[:60])

# ── Print report ─────────────────────────────────────────────────────────────
print()
print('=' * 68)
print('CORA DIAGNOSTIC REPORT')
print('=' * 68)
counts = {'OK':0,'WARN':0,'FAIL':0}
for status, label, detail in results:
    counts[status] += 1
    icon = {'OK':'[OK]  ','WARN':'[WARN]','FAIL':'[FAIL]'}[status]
    print(icon + ' ' + label)
    if detail:
        d = detail if len(detail) <= 120 else detail[:117] + '...'
        print('       ' + d)
print()
print('Summary: ' + str(counts['OK']) + ' OK  |  ' + str(counts['WARN']) + ' warnings  |  ' + str(counts['FAIL']) + ' failures')
print('=' * 68)
sys.exit(0 if counts['FAIL'] == 0 else 1)
