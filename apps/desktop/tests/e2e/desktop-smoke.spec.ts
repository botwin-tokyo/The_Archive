import {test,expect} from '@playwright/test';
test('offline bundled renderer exposes no native APIs',async({page})=>{await page.goto('/?mode=demo');await expect(page.getByRole('heading',{name:'THE ARCHIVE'})).toBeVisible();expect(await page.evaluate(()=>typeof (window as unknown as {require?:unknown}).require)).toBe('undefined');await expect(page.getByText('No real storage connected',{exact:true})).toBeVisible();});

test('unconfigured production renderer never falls back to demo data',async({page})=>{await page.goto('/');await expect(page.getByText('SERVICE MODE',{exact:true})).toBeVisible();await expect(page.getByText('Service unavailable or unconfigured',{exact:true})).toBeVisible();await expect(page.getByText('DEMO DATA',{exact:true})).toHaveCount(0);});
