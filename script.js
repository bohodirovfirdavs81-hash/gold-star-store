
document.addEventListener("DOMContentLoaded", () => {
  const buttons = document.querySelectorAll(".item");

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      const product = button.innerText.trim();

      alert("Вы выбрали: " + product);
    });
  });

  const balanceButton = document.querySelector(".gold");

  if (balanceButton) {
    balanceButton.addEventListener("click", () => {
      alert("Пополнение баланса скоро будет доступно!");
    });
  }
});
