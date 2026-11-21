import React from 'react';
import {createRoot} from 'react-dom/client';
import {FeatureBoundary} from './AppErrorBoundary';
import App from './App';
import {selectedMode} from './createDependencies';
import '../styles/base.css';
createRoot(document.getElementById('root')!).render(<React.StrictMode><FeatureBoundary name="Application" mode={selectedMode()}><App/></FeatureBoundary></React.StrictMode>);
