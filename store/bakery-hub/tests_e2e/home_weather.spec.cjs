// Synthetic forecasts and articles only. No external weather / RSS / Gemini calls.
const {test, expect} = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const auth = () => JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR,'auth.json')));
function fixture() {
    const date = auth().day;
    const days = Array.from({length:7}, (_,i) => {
        const d = new Date(date+'T00:00:00+09:00'); d.setUTCDate(d.getUTCDate()+i);
        const day = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Tokyo',year:'numeric',month:'2-digit',day:'2-digit'}).format(d);
        return {date:day, weather_code:[2,61,0,3,61,0,2][i], temperature_max:24+i%3, temperature_min:15+i%2,
            precipitation_probability:null, complete:i>0&&i<3, temperature_basis:'時刻別予報の取得分',
            from_at:day+'T'+(i===0?'12':'00')+':00+09:00', to_at:day+'T'+(i>2?'18':'23')+':00+09:00', representative_at:day+'T12:00+09:00'};
    });
    return {available:true, weather:'晴れ・曇り', weather_code:2, location:'広島市周辺（合成データ）',
        source:'MET Norway 予報',credit_url:'https://creativecommons.org/licenses/by/4.0/',
        valid_at:date+'T12:00+09:00',fetched_at:date+'T12:05+09:00',forecast_days:days};
}
async function setup(page, context, guest=false) {
    await context.route(/^https?:\/\//, route => new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
    await page.route('**/api/home/weather', route=>route.fulfill({json:fixture()}));
    await page.route('**/api/bakery-news', route=>route.fulfill({json:{articles:[{title:'合成ニュース：パンづくりの情報',url:'https://example.com/synthetic-news',published:auth().day+'T10:00:00+09:00'}]}}));
    await page.goto('/login');
    if(guest) await page.locator('#guest-demo-start').click();
    else {
        await page.locator('#username').fill(auth().username);
        await page.locator('#password').fill(auth().password);
        await page.locator('#admin-login-form button[type=submit]').click();
    }
    await expect(page).toHaveURL(/\/home/);
    await expect(page.locator('#operations-weather-status')).toHaveText('晴れ・曇り');
}
async function blankClick(page, id) {
    const card=page.locator(id); await card.scrollIntoViewIfNeeded();
    const box=await card.boundingBox();
    await card.click({position:{x:box.width-9,y:box.height-9}});
}
for (const size of [{width:390,height:844,name:'phone'}, {width:1280,height:800,name:'tablet'}, {width:1280,height:900,name:'pc'}]) {
    test(`${size.name}: card click, native weather keyboard, seven-day coverage, screenshots`, async ({page,context})=>{
        await page.setViewportSize(size);
        let gets=0, posts=0;
        page.on('request',r=>{if(r.url().endsWith('/api/home/weather'))gets++; if(r.url().endsWith('/api/home/weather/record'))posts++;});
        await setup(page,context);
        await expect(page.locator('#operations-weather-week li')).toHaveCount(7);
        await expect(page.locator('#operations-weather-week')).not.toBeVisible();
        await page.evaluate(()=>window.scrollTo(0,0));
        await page.screenshot({path:test.info().outputPath(`${size.name}-home.png`),fullPage:true});
        const summary=page.locator('#weather summary'); await summary.focus(); await page.keyboard.press('Space');
        await expect(page.locator('#weather')).toHaveAttribute('open','');
        await expect(page.locator('#operations-weather-today')).toContainText('取得分最高 24℃');
        await expect(page.locator('#operations-weather-today')).toContainText('降水確率 未提供');
        await expect(page.locator('#operations-weather-time')).toContainText('JST');
        expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
        await page.evaluate(()=>window.scrollTo(0,0));
        await summary.evaluate(node=>node.blur());
        if(size.name!=='pc') await page.screenshot({path:test.info().outputPath(`${size.name}-weekly.png`),fullPage:true});
        await summary.focus(); await page.keyboard.press('Enter'); await page.keyboard.press('Enter');
        expect(gets).toBe(1); expect(posts).toBe(1); // toggling does not fetch or record again
        await blankClick(page,'#next'); await expect(page).toHaveURL(/\/schedule$/);
        await page.goto('/home'); await blankClick(page,'#schedule'); await expect(page).toHaveURL(/\/schedule$/);
        await page.goto('/home'); await page.locator('#schedule .operations-card-heading a').click(); await expect(page).toHaveURL(/\/schedule\/new$/);
        await page.goto('/home'); await blankClick(page,'#today-staff'); await expect(page).toHaveURL(/\/staff-shifts$/);
        await page.goto('/home'); await page.locator('.operations-staff-register').click(); await expect(page).toHaveURL(/\/staff-shifts\/staff\/new$/);
        await page.goto('/home'); await blankClick(page,'#bakery-news'); await expect(page).toHaveURL(/\/bakery-news$/);
        await page.goto('/home'); await page.locator('#today-sales').focus(); await page.keyboard.press('Enter'); await expect(page).toHaveURL(/\/dashboard$/);
    });
}
test('article link wins over card; Guest restrictions and failed/missing forecasts remain usable', async ({page,context})=>{
    await page.setViewportSize({width:390,height:844}); await setup(page,context,true);
    await expect(page.locator('#today-staff')).toHaveCount(0);
    await expect(page.locator('#puoppo form')).toHaveCount(0);
    await expect(page.locator('#puoppo')).toContainText('ゲストデモでは');
    const popupPromise=page.waitForEvent('popup');
    await page.locator('#bakery-news-list a').click();
    const popup=await popupPromise; await popup.waitForLoadState().catch(()=>{}); await popup.close();
    await expect(page).toHaveURL(/\/home$/);
    await page.route('**/api/home/weather',r=>r.fulfill({json:{available:false,reason:'店舗の緯度経度が未設定です。'}}));
    await page.reload(); await expect(page.locator('#operations-weather-status')).toContainText('未設定');
    await page.locator('#weather summary').click(); await expect(page.locator('#operations-weather-week li')).toHaveCount(0);
    await page.route('**/api/home/weather',r=>r.abort()); await page.reload();
    await expect(page.locator('#operations-weather-status')).toContainText('取得できません');
    await blankClick(page,'#schedule'); await expect(page).toHaveURL(/\/schedule$/);
});


test('schedule item links stay above the main card link and main link supports keyboard', async ({page,context})=>{
    await setup(page,context);
    const day=auth().day;
    await page.route('**/api/home/schedule',r=>r.fulfill({json:{date:day,schedules:[{
        title:'合成の予定',start:day+'T23:59:00+09:00',end:null,url:'/schedule?date='+day,
    }]}}));
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
    await expect(page.locator('#operations-schedule-list a')).toHaveText('23:59　合成の予定');
    await page.locator('#operations-schedule-list a').click();
    await expect(page).toHaveURL(new RegExp('/schedule\\?date='+day+'$'));
    await page.goto('/home');
    await page.locator('#next .operations-card-primary').focus();
    await expect(page.locator('#next .operations-card-primary')).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/\/schedule$/);
});
