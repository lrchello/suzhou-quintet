// Progressive enhancement: navigation, forms and checkout also work without JS.
document.querySelectorAll('[data-checkout]').forEach(form => {
  const quantity = form.querySelector('[name="quantity"]');
  const total = form.querySelector('[data-total]');
  const update = () => {
    total.textContent = '¥' + (Number(form.dataset.price) * Number(quantity.value) / 100).toFixed(2);
  };
  quantity.addEventListener('change', update);
  update();
});
document.querySelectorAll('[data-print]').forEach(button => {
  button.addEventListener('click', () => window.print());
});
