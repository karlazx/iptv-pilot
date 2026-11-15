/* DOM + real isolated API integration; this is not a browser decoder test. */
const { JSDOM } = require('jsdom');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const base = process.env.UI_TEST_BASE;
assert.ok(base && /^http:\/\/127\.0\.0\.1:\d+$/.test(base), 'isolated local API required');
const wait = async predicate => {
  for (let i=0; i<100; i++) { if (predicate()) return; await new Promise(r=>setTimeout(r,30)); }
  throw new Error('UI condition timed out');
};
(async()=>{
  const dom = new JSDOM(fs.readFileSync('iptv_pilot/web/static/index.html','utf8'),
    {url:base+'/#/recordings',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window, requests=[];
  try {
    w.fetch=(url,options)=>{requests.push({url:String(url),options});return fetch(new URL(url,base),options);};
    const timer=w.setTimeout.bind(w);
    w.setTimeout=(fn,ms,...args)=>timer(fn,ms===10000?50:ms,...args);
    w.eval(fs.readFileSync('iptv_pilot/web/static/app.js','utf8').split('window.addEventListener("hashchange", renderRoute);')[0]
      + ';Object.assign(window,{renderRoute,openSubscriptionForm});');
    await w.renderRoute();
    assert.equal(w.document.querySelectorAll('#recording-body tr').length,50);
    await wait(()=>requests.some(r=>r.url==='/api/recordings/status'));
    assert.ok(requests.filter(r=>r.url==='/api/recordings/status').every(r=>JSON.parse(r.options.body).ids.length<=50));
    w.document.querySelector('[data-list-next]').click();
    await wait(()=>w.document.querySelector('#library-filter')?.textContent.includes('第 2 页'));
    w.document.querySelector('[data-list-prev]').click();
    await wait(()=>w.document.querySelector('#library-filter')?.textContent.includes('第 1 页'));
    const form=w.document.querySelector('#library-filter');form.elements.q.value='[needle]';
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
    await wait(()=>w.document.querySelector('#recording-body')?.textContent.includes('[needle]'));
    assert.equal(w.document.querySelectorAll('#recording-body tr').length,1);
    for (const [route,selector] of [['subscriptions','[data-edit-sub]'],['channels','.admin-check'],
      ['sources','[data-source-toggle]'],['bookmarks','[data-remove-bookmark]'],['summaries','[data-summary-detail]'],['system','.push-event-table tbody tr']]) {
      w.location.hash='#/'+route;await w.renderRoute();
      assert.ok(w.document.querySelector(selector),`${route} did not render`);
      assert.ok(w.document.querySelector('#library-filter'),`${route} has no paging controls`);
      assert.ok(!w.document.querySelector('.error-panel'),`${route} errored`);
    }
    w.location.hash='#/subscriptions';await w.renderRoute();await w.openSubscriptionForm();
    const sub=w.document.querySelector('#subscription-form');sub.elements.name.value='Clock subscription';
    sub.elements.match_type.value='channel_schedule';sub.elements.match_type.dispatchEvent(new w.Event('change'));
    sub.elements.channel_id.value='c000';sub.elements.rule_timezone.value='Europe/London';
    sub.querySelector('[name=weekday]').checked=true;
    for(const name of ['asr_enabled','live_asr_enabled','llm_enabled','push_enabled'])sub.elements[name].checked=false;
    w.document.querySelector('#preview-subscription').click();
    await wait(()=>w.document.querySelector('#subscription-preview').textContent.includes('Europe/London'));
    sub.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
    await wait(()=>w.document.querySelector('#modal').hidden);
    const created=await (await fetch(base+'/api/subscriptions')).json();
    const clock=created.find(s=>s.name==='Clock subscription');
    assert.equal(clock.match_type,'channel_schedule');assert.equal(clock.time_rule.duration_sec,3600);
    w.location.hash='#/recordings';const old=w.renderRoute();
    w.location.hash='#/summaries';await w.renderRoute();await old;
    assert.ok(w.document.querySelector('#summary-grid'),'stale route replaced current view');
    assert.ok(!requests.some(r=>r.url.includes('/api/recordings?limit=200')));
    console.log('Web integration passed: paging, full-list search, all management views, bounded refresh, calendar preview/create, stale-request protection.');
  } finally {w.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
