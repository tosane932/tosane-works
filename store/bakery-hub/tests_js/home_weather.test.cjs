const {test} = require('node:test');
const assert = require('node:assert/strict');
const {day, timestamp, icon} = require('../static/home_weather.js');
test('partial forecasts never claim full-day extremes; missing probability is not zero', () => {
    const view = day({date:'2026-10-20', complete:false, temperature_max:24, temperature_min:NaN, precipitation_probability:null, from_at:'2026-10-20T12:00+09:00', to_at:'2026-10-20T18:00+09:00'});
    assert.match(view.temperature, /取得分最高 24℃.*取得分最低 未提供/);
    assert.match(view.probability, /未提供/);
    assert.match(view.coverage, /時間帯限定/);
    assert.equal(view.date, '10/20(火)');
});
test('JST rendering and genuine zero percent / zero degrees remain visible', () => {
    assert.match(timestamp('2026-10-19T15:30:00Z'), /2026\/10\/20 00:30 JST/);
    assert.equal(timestamp(null), '未提供');
    const view=day({date:'2026-10-20',complete:true,temperature_max:0,temperature_min:-4,precipitation_probability:0,probability_basis:'日内最大'});
    assert.match(view.temperature, /予想最高 0℃.*最低 -4℃/);
    assert.equal(view.probability, '降水確率 0%（日内最大）');
    assert.equal(icon(95), 'storm'); assert.equal(icon(71), 'snow'); assert.equal(icon(61), 'rain'); assert.equal(icon(0), 'sun');
});

test('unknown weather does not invent a cloud icon or a condition', () => {
    assert.equal(icon(4), null);
    assert.equal(day({date:'2026-10-20',weather_code:4}).weather, '天気未提供');
});
