async function enterCash(page, value) {
    await page.locator('[data-cash-key="clear"]').click();
    for (const digit of value) { await page.locator(`[data-cash-key="${digit}"]`).click(); }
}
async function openTools(page) {
    const tools = page.locator('.register-tools');
    if (!await tools.evaluate(node => node.open)) { await tools.locator('summary').click(); }
}
module.exports = { enterCash, openTools };
