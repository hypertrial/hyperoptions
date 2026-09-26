declare module "plotly.js-dist-min" {
  const Plotly: {
    react: (root: HTMLElement, data: object[], layout?: object, config?: object) => void
    purge: (root: HTMLElement) => void
  }
  export default Plotly
}
