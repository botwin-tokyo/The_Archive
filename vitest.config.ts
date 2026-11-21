import { defineConfig } from 'vitest/config';
export default defineConfig({test:{maxWorkers:2,testTimeout:20000,environment:'jsdom',include:['apps/desktop/**/*.test.{ts,tsx}'],globals:true}});
