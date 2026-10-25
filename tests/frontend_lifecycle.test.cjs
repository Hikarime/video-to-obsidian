const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { Blob } = require('node:buffer');

function createPage(beaconAccepted = true, initialStorage = {}, initialSessionStorage = {}) {
  const elements = new Map(), events = new Map(), documentEvents = new Map(), streams = [], beacons = [], requests = [], storage = new Map(Object.entries(initialStorage)), sessionStorage = new Map(Object.entries(initialSessionStorage)), writes = [], timers = [];
  const config = { token:'test-token', client:'a'.repeat(32) };
  const element = id => {
    if(!elements.has(id)) elements.set(id,{id,value:'',textContent:'',hidden:false,disabled:false,handlers:{},attributes:{},parentElement:null,
      addEventListener(name,handler) { this.handlers[name] = handler; },replaceChildren() {},append() {},
      setAttribute(name,value) { this.attributes[name] = value; },getAttribute(name) { return this.attributes[name]; },
      focus() { document.activeElement = this; },
      contains(target) { for(let current = target; current; current = current.parentElement) if(current === this) return true; return false; }});
    return elements.get(id);
  };
  const document = {getElementById:element,addEventListener:(name,handler)=>documentEvents.set(name,handler),activeElement:null};
  for(const id of ['gallery','galleryMenu']) element(id).parentElement = element('galleryPicker');
  for(const id of ['galleryBilibili','galleryYouTube']) element(id).parentElement = element('galleryMenu');
  function browserStorage(values) {
    return {get length(){return values.size;},key:index=>[...values.keys()][index]??null,
      getItem:key=>values.get(key)??null,setItem:(key,value)=>{writes.push({key,value});values.set(key,value);},removeItem:key=>values.delete(key)};
  }
  const window = {addEventListener:(name,handler)=>events.set(name,handler),close:()=>window.closed=true,
    location:{replace:url=>window.destination=url},localStorage:browserStorage(storage),sessionStorage:browserStorage(sessionStorage)};
  const context = vm.createContext({
    document,window,Blob,URLSearchParams,
    localStorage:window.localStorage,sessionStorage:window.sessionStorage,
    navigator:{sendBeacon:(url,body)=>{beacons.push({url,body});return beaconAccepted;}},
    EventSource:class {constructor(url){this.url=url;streams.push(this);}close(){this.closed=true;}},
    fetch:async (url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({count:0})};},
    setInterval:()=>1,clearInterval:()=>{},setTimeout:(callback,delay)=>{timers.push({callback,delay});return timers.length;},
  });
  const html = fs.readFileSync(path.join(__dirname,'../index.html'),'utf8');
  const script = html.match(/<script nonce="__NONCE__">([\s\S]*?)<\/script>/)[1].replace('__BOOTSTRAP__',JSON.stringify(config));
  vm.runInContext(script,context);
  return {element,document,window,events,documentEvents,streams,beacons,requests,storage,sessionStorage,writes,timers,config};
}

(async () => {
  let page = createPage();
  page.element('links').value='BV17x411w7KC';
  page.element('links').handlers.input();
  assert.equal(page.writes.length,0);
  page.events.get('pagehide')();
  assert.equal(page.beacons[0].url,'/api/client-close');
  assert.deepEqual(JSON.parse(await page.beacons[0].body.text()),{client:page.config.client,token:page.config.token});
  assert.equal(page.streams[0].closed,true);
  assert.equal(page.element('links').value,'');
  assert.equal(page.storage.size,0);
  assert.equal(page.writes.length,0);
  page.element('links').value='Browser-restored form text';
  page.events.get('pageshow')();
  assert.equal(page.element('links').value,'');
  assert.equal(page.streams.length,2);
  assert.equal(page.streams[1].closed,undefined);

  page = createPage(false);
  page.events.get('pagehide')();
  const fallback = page.requests.find(request=>request.url==='/api/client-close');
  assert.equal(fallback.options.keepalive,true);
  assert.equal(JSON.parse(fallback.options.body).token,'test-token');

  page = createPage();
  page.element('links').value='Do not persist this input';
  await page.element('shutdown').handlers.click();
  assert.equal(page.element('links').value,'');
  assert.equal(page.writes.length,0);
  assert.equal(page.beacons[0].url,'/api/shutdown');
  assert.equal(page.streams[0].closed,true);
  assert.equal(page.window.closed,true);
  page.timers.find(timer=>timer.delay===120).callback();
  assert.equal(page.window.destination,'about:blank');
  page = createPage();
  assert.equal(page.element('galleryMenu').hidden,true);
  assert.equal(page.element('gallery').getAttribute('aria-expanded'),'false');
  for(const [id,platform] of [['galleryBilibili','bilibili'],['galleryYouTube','youtube']]) {
    page.element('gallery').handlers.click();
    assert.equal(page.element('galleryMenu').hidden,false);
    assert.equal(page.element('gallery').getAttribute('aria-expanded'),'true');
    assert.equal(page.document.activeElement,page.element('galleryBilibili'));
    page.documentEvents.get('click')({target:page.element('gallery')});
    assert.equal(page.element('galleryMenu').hidden,false);
    await page.element(id).handlers.click();
    const gallery = page.requests.filter(request=>request.url==='/api/open-gallery').at(-1);
    assert.equal(JSON.parse(gallery.options.body).platform,platform);
    assert.equal(gallery.options.headers['X-Bili-Token'],page.config.token);
    assert.equal(page.element('galleryMenu').hidden,true);
    assert.equal(page.element('gallery').getAttribute('aria-expanded'),'false');
    assert.equal(page.element('gallery').disabled,false);
  }
  function key(key) { return {key,preventDefault(){this.prevented=true;}}; }
  let event = key('ArrowUp');
  page.element('gallery').handlers.keydown(event);
  assert.equal(event.prevented,true);
  assert.equal(page.document.activeElement,page.element('galleryYouTube'));
  for(const [pressed,target] of [['ArrowDown','galleryBilibili'],['End','galleryYouTube'],['Home','galleryBilibili'],['ArrowUp','galleryYouTube']]) {
    page.element('galleryMenu').handlers.keydown(key(pressed));
    assert.equal(page.document.activeElement,page.element(target));
  }
  page.documentEvents.get('keydown')(key('Escape'));
  assert.equal(page.element('galleryMenu').hidden,true);
  assert.equal(page.document.activeElement,page.element('gallery'));
  page.element('gallery').handlers.click();
  page.documentEvents.get('click')({target:page.element('links')});
  assert.equal(page.element('galleryMenu').hidden,true);
  page.element('gallery').handlers.click();
  page.element('galleryMenu').handlers.keydown(key('Tab'));
  assert.equal(page.element('galleryMenu').hidden,true);
  page = createPage(true,{
    'obsidian-video-collection.draft.collection':'old draft',
    'obsidian-video-collection.job.collection':'old job',
    'bili.draft.old-vault':'older draft',
    'video-to-obsidian.draft.old-vault':'another old draft',
    'another-app.draft':'keep this',
  },{'bili.draft.old-vault':'old session draft','another-app.preference':'keep this too'});
  assert.equal(page.element('links').value,'');
  assert.deepEqual([...page.storage.entries()],[['another-app.draft','keep this']]);
  assert.deepEqual([...page.sessionStorage.entries()],[['another-app.preference','keep this too']]);
  assert.equal(page.writes.length,0);
  assert.equal(page.requests.some(request=>request.url.startsWith('/api/jobs/')),false);
  console.log('Frontend privacy, lifecycle and Gallery menu checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
