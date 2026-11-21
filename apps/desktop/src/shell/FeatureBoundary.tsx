import { Component, type ReactNode } from 'react';
import { PanelFallback } from '../shared/ui';

export class FeatureBoundary extends Component<{ name: string; children: ReactNode; resetKey?: string }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidUpdate(previous: Readonly<{ name: string; children: ReactNode; resetKey?: string }>) {
    if (previous.resetKey !== this.props.resetKey && this.state.failed) this.setState({ failed: false });
  }
  render() {
    return this.state.failed ? <PanelFallback name={this.props.name} onRetry={() => this.setState({ failed: false })}/> : this.props.children;
  }
}
