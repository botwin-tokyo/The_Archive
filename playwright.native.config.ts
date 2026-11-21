import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'apps/desktop/tests/e2e',testMatch:'native-smoke.spec.ts',timeout:45000,workers:1,outputDir:'test-results/native'});
