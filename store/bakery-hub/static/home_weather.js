/* Presentation helpers: no network requests or persistence, usable in Node tests. */
(function (root) {
    'use strict';
    const dates = new Intl.DateTimeFormat('ja-JP', {timeZone:'Asia/Tokyo', month:'numeric', day:'numeric', weekday:'short'});
    const times = new Intl.DateTimeFormat('ja-JP', {timeZone:'Asia/Tokyo', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
    const number = value => typeof value === 'number' && Number.isFinite(value);
    function icon(code) {
        if ([95,96,99].includes(code)) return 'storm';
        if ([71,73,75,77,85,86].includes(code)) return 'snow';
        if ([51,53,55,56,57,61,63,65,66,67,80,81,82].includes(code)) return 'rain';
        if (code === 0) return 'sun';
        if ([1,2].includes(code)) return 'partly';
        return [3,45,48].includes(code) ? 'cloud' : null;
    }
    function condition(code) {
        if (!Number.isInteger(code)) return '天気未提供';
        if (code === 0) return '晴れ';
        if ([1,2].includes(code)) return '晴れ・曇り';
        if (code === 3) return '曇り';
        if ([45,48].includes(code)) return '霧';
        return ({rain:'雨', snow:'雪', storm:'雷雨'})[icon(code)] || '天気未提供';
    }
    function timestamp(value) {
        const date = new Date(value);
        return Number.isNaN(date.valueOf()) || !value ? '未提供' : times.format(date) + ' JST';
    }
    function day(row) {
        const date = new Date(row.date + 'T00:00:00+09:00');
        const high = number(row.temperature_max) ? row.temperature_max + '℃' : null;
        const low = number(row.temperature_min) ? row.temperature_min + '℃' : null;
        return {
            weather: row.weather_label || row.transition_label || condition(row.weather_code),
            icon: row.weather_icon || icon(row.weather_code),
            location: row.location || '',
            date: Number.isNaN(date.valueOf()) ? '日付未提供' : dates.format(date),
            temperature: row.complete && high !== null && low !== null ? '最高 ' + high + ' ／ 最低 ' + low : '',
            probability: '降水確率 ' + (number(row.precipitation_probability) ? row.precipitation_probability + '%' : '—') + (row.probability_basis ? '（' + row.probability_basis + '）' : ''),
        };
    }
    const api = {icon, condition, timestamp, day};
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.HomeWeather = api;
})(typeof window === 'undefined' ? globalThis : window);
