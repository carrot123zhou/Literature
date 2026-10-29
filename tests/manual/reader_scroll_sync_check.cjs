const {chromium} = require(process.env.PW_PACKAGE_PATH);
(async () => {
  const browser = await chromium.launch({headless:true});
  const checks = [], errors = [], mutations = [];
  const ok = (name, pass, detail) => checks.push({name, pass:!!pass, detail});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:920}});
    page.on('pageerror', e => errors.push(String(e)));
    page.on('request', r => { if(r.method() === 'POST') mutations.push(r.url()); });
    await page.goto(`http://127.0.0.1:${process.argv[2]}/#/p/${process.argv[3]}/reader`);
    await page.waitForSelector('#rdrr-pane-source p[data-block-id="b-p-1"]');
    await page.waitForTimeout(1200);
    const pane = side => page.locator(`#rdrr-pane-${side} .rdrr-pane-body`);
    const state = () => page.evaluate(() => Object.fromEntries(['source','trans'].map(side => {
      const b=document.querySelector(`#rdrr-pane-${side} .rdrr-pane-body`);
      const p=b.querySelector('[data-block-id="b-p-1"]'), r=p.getBoundingClientRect();
      return [side,{y:b.scrollTop,ratio:(b.getBoundingClientRect().top+b.clientHeight*.3-r.top)/r.height,height:r.height}];
    })));
    const position = (side,ratio) => pane(side).evaluate((b,ratio) => {
      const p=b.querySelector('[data-block-id="b-p-1"]'), r=p.getBoundingClientRect();
      b.scrollTop += r.top-b.getBoundingClientRect().top+r.height*ratio-b.clientHeight*.3;
    },ratio);
    await position('source',.3); await page.waitForTimeout(100);
    let s=await state();
    ok('不同高度长段按段内比例对齐',Math.abs(s.source.ratio-s.trans.ratio)<.002 && Math.abs(s.source.height-s.trans.height)>100,s);
    const box=await pane('source').boundingBox();
    await page.mouse.move(box.x+box.width/2,box.y+box.height/2);
    const samples=[];
    for(let i=0;i<8;i++) {
      await page.mouse.wheel(0,50); await page.waitForTimeout(45); samples.push(await state());
    }
    ok('连续滚轮每次都推动译文（无全局锁停顿）',samples.every((x,i)=>!i||x.trans.y>samples[i-1].trans.y+1),samples);
    ok('长段滚动全过程比例一致',samples.every(x=>Math.abs(x.source.ratio-x.trans.ratio)<.002));
    const before=await state(), tbox=await pane('trans').boundingBox();
    await page.mouse.move(tbox.x+tbox.width/2,tbox.y+tbox.height/2);
    await page.mouse.wheel(0,140); await page.waitForTimeout(80);
    s=await state();
    ok('译文可立即反向接管',s.source.y>before.source.y+5&&Math.abs(s.source.ratio-s.trans.ratio)<.002,s);
    const stable=await state(); await page.waitForTimeout(700); s=await state();
    ok('停止滚动后无回弹振荡',Math.abs(s.source.y-stable.source.y)<1&&Math.abs(s.trans.y-stable.trans.y)<1,{stable,s});
    await pane('source').evaluate(b=>{b.scrollTop=0;});await page.waitForTimeout(80);
    ok('页首对齐',(await state()).trans.y===0);
    await pane('source').evaluate(b=>{b.scrollTop=b.scrollHeight;});await page.waitForTimeout(80);
    ok('页尾对齐',await pane('trans').evaluate(b=>Math.abs(b.scrollHeight-b.clientHeight-b.scrollTop)<1));
    await position('source',.4);await page.waitForTimeout(100);
    await page.getByRole('button',{name:'Aa',exact:true}).click();
    const follow=page.locator('.rdrr-aa label').filter({hasText:'跟随滚动'}).locator('input');
    await follow.uncheck();await page.getByRole('button',{name:'Aa',exact:true}).click();await page.waitForTimeout(100);
    const off=await state();await position('source',.5);await page.waitForTimeout(250);s=await state();
    ok('关闭跟随只滚当前栏',s.source.y!==off.source.y&&s.trans.y===off.trans.y,{off,s});
    await page.getByRole('button',{name:'Aa',exact:true}).click();await follow.check();
    await page.getByRole('button',{name:'Aa',exact:true}).click();await page.waitForTimeout(100);
    await position('source',.6);await page.waitForTimeout(100);s=await state();
    ok('重新开启恢复比例跟随',Math.abs(s.source.ratio-s.trans.ratio)<.002,s);
    // 越过段落边界后仍按新块定位；缺译块保留相同 ID 的原文回退。
    for (const id of ['b-p-2','b-p-3']) {
      await pane('source').evaluate((b,id)=>{
        const r=b.querySelector(`[data-block-id="${id}"]`).getBoundingClientRect();
        b.scrollTop+=r.top-b.getBoundingClientRect().top+r.height*.35-b.clientHeight*.3;
      },id);
      await page.waitForTimeout(100);
      const mapped=await page.evaluate(id=>['source','trans'].map(side=>{
        const b=document.querySelector(`#rdrr-pane-${side} .rdrr-pane-body`),r=b.querySelector(`[data-block-id="${id}"]`).getBoundingClientRect();
        return (b.getBoundingClientRect().top+b.clientHeight*.3-r.top)/r.height;
      }),id);
      ok(id==='b-p-2'?'跨段保持新块比例':'缺译回退块保持比例',Math.abs(mapped[0]-mapped[1])<.002,mapped);
    }
    await page.getByRole('button',{name:/目录/}).first().click();
    await page.locator('.rdrr-toc-row').first().click();await page.waitForTimeout(700);
    ok('目录点击仍可同时回到首章',(await state()).source.y<10&&(await state()).trans.y<10);
    await page.getByRole('checkbox',{name:'trans',exact:true}).uncheck();await page.waitForTimeout(100);
    await position('source',.45);await page.waitForTimeout(100);
    ok('关闭译文栏后原文仍可独立滚动',(await state()).source.y>500);
    await page.getByRole('checkbox',{name:'trans',exact:true}).check();await page.waitForTimeout(100);
    await position('source',.55);await page.waitForTimeout(150);s=await state();
    ok('重开译文栏后恢复跟随',Math.abs(s.source.ratio-s.trans.ratio)<.002,s);
    await pane('source').evaluate(b=>{
      const r=b.querySelector('[data-block-id="b-p-3"]').getBoundingClientRect();
      b.scrollTop+=r.top-b.getBoundingClientRect().top+r.height*.4-b.clientHeight*.3;
    });await page.waitForTimeout(1400);
    const pdf=await pane('origin').evaluate(b=>{
      const box=b.querySelector('[data-block-id="b-p-3"]'),r=box?.getBoundingClientRect(),v=b.getBoundingClientRect();
      return {y:b.scrollTop,error:r?Math.abs(r.top+r.height/2-v.top-v.height/2):null};
    });
    ok('跨页后PDF仍跟随对应版面框',pdf.y>100&&pdf.error!==null&&pdf.error<2,pdf);
    ok('无脚本错误或模型请求',errors.length===0&&mutations.length===0,{errors,mutations});
  } catch(e) { ok('runner',false,String(e.stack||e)); }
  finally { await browser.close(); }
  console.log(JSON.stringify({checks,passed:checks.filter(x=>x.pass).length,failed:checks.filter(x=>!x.pass).length},null,2));
  process.exitCode=checks.some(x=>!x.pass)?1:0;
})();
