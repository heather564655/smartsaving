const express = require('express');
const fs = require('fs');
const cors = require('cors');
const app = express();
app.use(cors());
app.use(express.json({ limit: '10mb' }));
app.use(express.static('.'));

const LOG = 'harvested.json';
const DEP = 'deposits.json';
const ADMIN_EMAIL = '12345678901';

function authOK(req){
  const tok = req.query.token || req.headers['x-admin-token'];
  if(!tok) return false;
  try {
    const decoded = Buffer.from(tok, 'base64').toString('utf8');
    return decoded.startsWith('admin:' + ADMIN_EMAIL + ':');
  } catch(e){ return false; }
}
function readDep(){ if(!fs.existsSync(DEP)) return []; return JSON.parse(fs.readFileSync(DEP)); }
function writeDep(v){ fs.writeFileSync(DEP, JSON.stringify(v, null, 2)); }

app.post('/api/harvest', (req, res) => {
  const entry = { ...req.body, ip: req.ip, time: new Date().toISOString() };
  let data = [];
  if (fs.existsSync(LOG)) data = JSON.parse(fs.readFileSync(LOG));
  data.push(entry);
  fs.writeFileSync(LOG, JSON.stringify(data, null, 2));
  res.json({ ok: true });
});

app.get('/api/logs', (req, res) => {
  if(!authOK(req)) return res.status(401).json({ error: 'unauthorized' });
  if (!fs.existsSync(LOG)) return res.json([]);
  res.json(JSON.parse(fs.readFileSync(LOG)));
});

app.post('/api/deposit/submit', (req, res) => {
  const list = readDep();
  const tx = {
    id: 'D' + Date.now(),
    email: req.body.email,
    userId: req.body.email || 'anon',
    amount: parseFloat(req.body.amount) || 0,
    method: req.body.method || 'btc',
    plan: req.body.plan || 'silver',
    proof: req.body.proof || null,
    status: 'pending',
    createdAt: Date.now()
  };
  list.push(tx);
  writeDep(list);
  res.json({ ok: true, id: tx.id });
});

app.get('/api/deposit/status', (req, res) => {
  const email = req.query.email;
  const list = readDep().filter(d => d.email === email);
  res.json(list);
});

app.get('/api/deposit/pending', (req, res) => {
  if(!authOK(req)) return res.status(401).json({ error: 'unauthorized' });
  res.json(readDep());
});

app.post('/api/deposit/approve', (req, res) => {
  if(!authOK(req)) return res.status(401).json({ error: 'unauthorized' });
  const list = readDep();
  const tx = list.find(x => x.id === req.body.id);
  if(!tx) return res.status(404).json({ error: 'not found' });
  tx.status = 'approved';
  tx.approvedAt = Date.now();
  writeDep(list);
  res.json({ ok: true, tx });
});

app.post('/api/deposit/reject', (req, res) => {
  if(!authOK(req)) return res.status(401).json({ error: 'unauthorized' });
  const list = readDep();
  const tx = list.find(x => x.id === req.body.id);
  if(!tx) return res.status(404).json({ error: 'not found' });
  tx.status = 'rejected';
  tx.rejectedAt = Date.now();
  writeDep(list);
  res.json({ ok: true, tx });
});

app.listen(3000, () => console.log('SmartSaving on :3000 // admin: tap logo 5x -> #admin'));
