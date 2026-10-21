const {test} = require('node:test');
const assert = require('node:assert/strict');
const {day, timestamp, icon, condition} = require('../static/home_weather.js');
test('partial forecasts do not show incomplete temperature extremes; missing probability is not zero', () => {
    const view = day({date:'2026-10-20', complete:false, temperature_max:24, temperature_min:NaN, precipitation_probability:null, from_at:'2026-10-20T12:00+09:00', to_at:'2026-10-20T18:00+09:00'});
    assert.equal(view.temperature, '');
    assert.equal(view.probability, '降水確率 —');
    assert.equal(view.date, '10/20(火)');
});
test('JST rendering and genuine zero percent / zero degrees remain visible', () => {
    assert.match(timestamp('2026-10-19T15:30:00Z'), /2026\/10\/20 00:30 JST/);
    assert.equal(timestamp(null), '未提供');
    const view=day({date:'2026-10-20',complete:true,temperature_max:0,temperature_min:-4,precipitation_probability:0,probability_basis:'日内最大'});
    assert.match(view.temperature, /最高 0℃.*最低 -4℃/);
    assert.equal(view.probability, '降水確率 0%（日内最大）');
    assert.equal(icon(95), 'storm'); assert.equal(icon(71), 'snow'); assert.equal(icon(61), 'rain'); assert.equal(icon(0), 'sun');
});

test('JMA period, area and official weather label are displayed without inventing extremes', () => {
    const view=day({date:'2026-10-21',weather_label:'曇り時々雨',weather_icon:'rain',
        location:'広島県南部',precipitation_probability:0,probability_basis:'12〜18時',complete:false});
    assert.equal(view.weather, '曇り時々雨');
    assert.equal(view.icon, 'rain');
    assert.equal(view.probability, '降水確率 0%（12〜18時）');
    assert.equal(view.location, '広島県南部');
    assert.equal(view.temperature, '');
});

test('unknown weather does not invent a cloud icon or a condition', () => {
    assert.equal(icon(4), null);
    assert.equal(day({date:'2026-10-20',weather_code:4}).weather, '天気未提供');
});

test('seven weather categories have matching icon and text; transitions require server evidence', () => {
    for (const [code, expectedIcon, expectedText] of [
        [0,'sun','晴れ'], [2,'partly','晴れ・曇り'], [3,'cloud','曇り'],
        [61,'rain','雨'], [71,'snow','雪'], [95,'storm','雷雨']
    ]) {
        assert.equal(icon(code), expectedIcon);
        assert.equal(condition(code), expectedText);
        assert.equal(day({date:'2026-10-20',weather_code:code}).weather, expectedText);
    }
    assert.equal(day({date:'2026-10-20',weather_code:61,transition_label:'晴れのち雨'}).weather, '晴れのち雨');
});
