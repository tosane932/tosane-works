const {test,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path');
for(const width of [390,800,1280]) test(`Guest prep ${width}`,async({page,request,browser})=>{
 const auth=JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR,'auth.json')));
 const headers={'X-E2E-Control':auth.control};
 await request.post('/__e2e/guest-register?enabled=1&prep=1',{headers});
 const other=await browser.newContext();let second;
 try{
  await page.setViewportSize({width,height:800});await page.goto('/login');await page.locator('#guest-demo-start').click();
  await page.goto('/prep/recipes');await page.locator('a[href*="source=register"]').first().click();
  await page.locator('[name=base_quantity]').fill('10');await page.locator('[name=ingredient_name]').fill('架空強力粉');
  await page.locator('[name=ingredient_amount]').fill('500');await page.getByRole('button',{name:'レシピを保存する'}).click();
  await page.goto('/prep');await page.locator('[name=recipe_id]').check();
  await page.locator('input[name^="quantity_"]').fill('20');await page.getByRole('button',{name:'まとめて保存'}).click();
  await page.reload();await expect(page.locator('input[name^="quantity_"]')).toHaveValue('20');
  await expect(page.locator('.prep-material')).toContainText('1000');
  second=await other.newPage();await second.goto('http://127.0.0.1:8765/login');await second.locator('#guest-demo-start').click();
  await second.goto('http://127.0.0.1:8765/prep/recipes');await expect(second.locator('main')).toContainText('レシピはまだ登録されていません');
  await request.post('/__e2e/guest-register?enabled=1&prep=0',{headers});expect((await page.request.get('/prep')).status()).toBe(403);
 }finally{
  await page.request.post('/__e2e/end-guest',{headers});if(second)await second.request.post('/__e2e/end-guest',{headers});
  await other.close();await request.post('/__e2e/guest-register?enabled=0',{headers});
 }
});
