// Synthetic forecasts and articles only; no external weather / RSS / Gemini calls.
const {test, expect} = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const auth = () => JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR, 'auth.json')));
function fixture(probability = null) {
    const date = auth().day;
    const days = Array.from({length:7}, (_, i) => {
        const d = new Date(date+'T00:00:00+09:00'); d.setUTCDate(d.getUTCDate()+i);
        const day = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Tokyo',year:'numeric',month:'2-digit',day:'2-digit'}).format(d);
        return {date:day, weather_code:[2,61,0,3,71,95,2][i], temperature_max:24+i%3, temperature_min:15+i%2,
            precipitation_probability:i===0?probability:null, probability_basis:'日内最大', complete:i>0&&i<3,
            temperature_basis:'時刻別予報の取得分', from_at:day+'T'+(i===0?'12':'00')+':00+09:00',
            to_at:day+'T'+(i>2?'18':'23')+':00+09:00', representative_at:day+'T12:00+09:00'};
    });
    return {available:true, weather:'晴れ・曇り', weather_code:2, location:'広島市周辺（合成データ）',
        source:'MET Norway 予報', credit_url:'https://creativecommons.org/licenses/by/4.0/',
        valid_at:date+'T12:00+09:00', fetched_at:date+'T12:05+09:00', forecast_days:days};
}
async function setup(page, context, guest=false) {
    await context.route(/^https?:\/\//, route => new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
    await page.route('**/api/home/weather', route=>route.fulfill({json:fixture()}));
    await page.route('**/api/home/weather/record', route=>route.fulfill({json:{saved:false}}));
    await page.route('**/api/bakery-news', route=>route.fulfill({json:{articles:[{title:'合成ニュース：パンづくりの情報',url:'https://example.com/synthetic-news',published:auth().day+'T10:00:00+09:00',source:'食品新聞'}]}}));
    await page.goto('/login');
    if (guest) await page.locator('#guest-demo-start').click();
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
    const box=await card.boundingBox(); await card.click({position:{x:box.width-9,y:box.height-9}});
}
for (const size of [{width:390,height:844,name:'phone'}, {width:1280,height:800,name:'tablet'}, {width:1280,height:900,name:'pc'}]) {
    test(`${size.name}: weather page, home links, news and screenshots`, async ({page,context})=>{
        await page.setViewportSize(size);
        await setup(page,context);
        await expect(page.locator('#operations-weather-probability')).toContainText('未提供'); // MET does not provide probability.
        await expect(page.locator('#weather summary')).toHaveCount(0);
        await expect(page.locator('#puoppo')).toHaveCount(0);
        await page.screenshot({path:test.info().outputPath(`${size.name}-home.png`),fullPage:true});
        if(size.name==='phone') {
            await page.locator('.operations-links').screenshot({path:test.info().outputPath('phone-work-links.png')});
        }
        await blankClick(page,'#weather'); await expect(page).toHaveURL(/\/weather$/);
        await expect(page.locator('#operations-weather-week li')).toHaveCount(7);
        await expect(page.locator('#operations-weather-today')).toContainText('取得分最高 24℃');
        await expect(page.locator('#operations-weather-today')).toContainText('降水確率 未提供');
        await expect(page.locator('#operations-weather-time')).toContainText('JST');
        await expect(page.locator('#weather')).toHaveAttribute('data-condition','partly');
        await expect(page.locator('#weather [data-weather-icon="partly"]')).toBeVisible();
        expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
        if(size.name!=='pc') await page.screenshot({path:test.info().outputPath(`${size.name}-weather.png`),fullPage:true});
        await page.locator('.weather-back').first().click(); await expect(page).toHaveURL(/\/home$/);
        await page.locator('#weather').focus(); await page.keyboard.press('Enter'); await expect(page).toHaveURL(/\/weather$/);
        await page.goBack(); await expect(page).toHaveURL(/\/home$/);
        await blankClick(page,'#next'); await expect(page).toHaveURL(/\/schedule$/);
        await page.goto('/home'); await blankClick(page,'#schedule'); await expect(page).toHaveURL(/\/schedule$/);
        await page.goto('/home'); await page.locator('#schedule .operations-card-heading a').click(); await expect(page).toHaveURL(/\/schedule\/new$/);
        await page.goto('/home'); await blankClick(page,'#today-staff'); await expect(page).toHaveURL(/\/staff-shifts$/);
        await page.goto('/home'); await page.locator('.operations-staff-register').click(); await expect(page).toHaveURL(/\/staff-shifts\/staff\/new$/);
        await page.goto('/home'); await blankClick(page,'#bakery-news'); await expect(page).toHaveURL(/\/bakery-news$/);
        if(size.name==='pc') await page.screenshot({path:test.info().outputPath('pc-news.png'),fullPage:true});
        await page.goto('/home'); await page.locator('#today-sales').focus(); await page.keyboard.press('Enter'); await expect(page).toHaveURL(/\/dashboard$/);
    });
}
test('guest and failure paths stay usable; work buttons navigate across full width', async ({page,context})=>{
    await page.setViewportSize({width:390,height:844}); await setup(page,context,true);
    await expect(page.locator('#today-staff')).toHaveCount(0);
    await expect(page.locator('#puoppo')).toHaveCount(0);
    const popupPromise=page.waitForEvent('popup'); await page.locator('#bakery-news-list a').click();
    const popup=await popupPromise; await popup.waitForLoadState().catch(()=>{}); await popup.close();
    await page.route('**/api/home/weather',r=>r.fulfill({json:{available:false,reason:'店舗の緯度経度が未設定です。'}}));
    await page.reload(); await expect(page.locator('#operations-weather-status')).toContainText('未設定');
    await blankClick(page,'#weather'); await expect(page).toHaveURL(/\/weather$/);
    await expect(page.locator('#operations-weather-week li')).toHaveCount(0);
    await page.route('**/api/home/weather',r=>r.abort()); await page.reload();
    await expect(page.locator('#operations-weather-status')).toContainText('取得できません');
    await page.locator('.weather-back').last().click(); await expect(page).toHaveURL(/\/home$/);
    await blankClick(page,'.operations-work-products'); await expect(page).toHaveURL(/\/products$/);
    await page.goto('/home'); await blankClick(page,'.operations-work-sales'); await expect(page).toHaveURL(/\/input$/);
    await page.goto('/home'); await blankClick(page,'.operations-work-memo'); await expect(page).toHaveURL(/\/material-orders$/);
});
test('home probability is shown only when supplied', async ({page,context})=>{
    await setup(page,context);
    await page.route('**/api/home/weather',r=>r.fulfill({json:fixture(0)}));
    await page.reload(); await expect(page.locator('#operations-weather-probability')).toHaveText('今日の最大降水確率：0%');
});
