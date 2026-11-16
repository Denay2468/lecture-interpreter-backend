'use strict';
const assert = require('assert');
const fs = require('fs');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<input id="course-pdfs" type="file" multiple><p id="materials-status"></p><ul id="materials-list"></ul><button id="materials-clear"></button><input id="legacy" type="checkbox"><select id="source-language"><option value="id">ID</option></select>', {runScripts:'outside-only'});
const w = dom.window, el = id => w.document.getElementById(id);
let finish, uploads = 0;
w.BLB = {api:{session:{get:()=>({user:{role:'LECTURER'}})}, upload:async()=>{uploads++;return new Promise(r=>finish=r);}}};
w.eval(fs.readFileSync(require('path').join(__dirname,'../js/course-materials.js'),'utf8'));
function choose(files) {
  Object.defineProperty(el('course-pdfs'),'files',{value:files,configurable:true});
  el('course-pdfs').dispatchEvent(new w.Event('change'));
}
const tick = () => new Promise(r=>setTimeout(r,0));
(async()=>{
  assert.equal(w.BLB.courseMaterials.settings().materials.length,0);
  choose(Array.from({length:11},()=>new w.File(['x'],'x.pdf')));
  assert.equal(uploads,0); assert.throws(()=>w.BLB.courseMaterials.settings(),/failed/);
  el('materials-clear').click();
  choose([{name:'large.pdf',size:10*1024*1024+1}]);
  assert.equal(uploads,0); assert(el('materials-status').textContent.includes('10 MB'));
  choose([new w.File(['%PDF'],'lesson.pdf')]);
  assert.equal(uploads,1);assert.throws(()=>w.BLB.courseMaterials.settings(),/Wait/);
  finish({documents:[{name:'<img src=x onerror=alert(1)>.pdf',text:'Network module',pages:2,pages_without_text:0}]});await tick();
  assert.equal(el('materials-list').querySelectorAll('img').length,0);
  assert.equal(w.BLB.courseMaterials.settings().materials[0].text,'Network module');
  const snapshot = w.BLB.courseMaterials.settings();
  el('materials-list').querySelector('button').click();
  assert.equal(w.BLB.courseMaterials.settings().materials.length,0);
  assert.equal(snapshot.materials.length,1);
  choose([new w.File(['%PDF'],'late.pdf')]);
  el('materials-clear').click();
  finish({documents:[{name:'late.pdf',text:'ignored',pages:1}]});await tick();
  assert.equal(w.BLB.courseMaterials.settings().materials.length,0);
  console.log('PASS: limits, pending upload guard, safe filenames, removal, immutable lecture snapshot, stale responses');
  w.close();
})().catch(e=>{console.error(e);process.exit(1);});
