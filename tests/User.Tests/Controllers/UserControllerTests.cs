using System.Collections.Generic;
using System.Linq;
using Microsoft.AspNetCore.Mvc;
using User.API.Controllers;
using User.API.Models;
using Xunit;

namespace User.API.Tests.Controllers
{
    public class UserControllerTests
    {
        public UserControllerTests()
        {
            // Reset static fields before each test
            typeof(UserController)
                .GetField("users", System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic)
                ?.SetValue(null, new List<UserModel>());
            typeof(UserController)
                .GetField("nextId", System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic)
                ?.SetValue(null, 1);
        }

        [Fact]
        public void GetUsers_ReturnsEmptyList_WhenNoUsersExist()
        {
            var controller = new UserController();
            var result = controller.GetUsers();
            var okResult = Assert.IsType<OkObjectResult>(result.Result);
            var users = Assert.IsAssignableFrom<IEnumerable<UserModel>>(okResult.Value);
            Assert.Empty(users);
        }

        [Fact]
        public void CreateUser_AddsUser_AndCanBeRetrieved()
        {
            var controller = new UserController();
            var user = new UserModel { Name = "Test", Email = "test@email.com" };
            var result = controller.CreateUser(user);
            var createdResult = Assert.IsType<CreatedAtActionResult>(result.Result);
            var createdUser = Assert.IsType<UserModel>(createdResult.Value);
            Assert.Equal(1, createdUser.Id);
            Assert.Equal("Test", createdUser.Name);
            Assert.Equal("test@email.com", createdUser.Email);

            // Now retrieve
            var getResult = controller.GetUser(1);
            var okResult = Assert.IsType<OkObjectResult>(getResult.Result);
            var retrievedUser = Assert.IsType<UserModel>(okResult.Value);
            Assert.Equal(createdUser.Id, retrievedUser.Id);
        }

        [Fact]
        public void GetUser_ReturnsNotFound_WhenUserDoesNotExist()
        {
            var controller = new UserController();
            var result = controller.GetUser(999);
            Assert.IsType<NotFoundResult>(result.Result);
        }

        [Fact]
        public void UpdateUser_UpdatesExistingUser()
        {
            var controller = new UserController();
            var user = new UserModel { Name = "Old", Email = "old@email.com" };
            controller.CreateUser(user);
            var updatedUser = new UserModel { Name = "New", Email = "new@email.com" };
            var result = controller.UpdateUser(1, updatedUser);
            Assert.IsType<NoContentResult>(result);
            var getResult = controller.GetUser(1);
            var okResult = Assert.IsType<OkObjectResult>(getResult.Result);
            var retrievedUser = Assert.IsType<UserModel>(okResult.Value);
            Assert.Equal("New", retrievedUser.Name);
            Assert.Equal("new@email.com", retrievedUser.Email);
        }

        [Fact]
        public void UpdateUser_ReturnsNotFound_WhenUserDoesNotExist()
        {
            var controller = new UserController();
            var updatedUser = new UserModel { Name = "New", Email = "new@email.com" };
            var result = controller.UpdateUser(999, updatedUser);
            Assert.IsType<NotFoundResult>(result);
        }

        [Fact]
        public void DeleteUser_RemovesUser()
        {
            var controller = new UserController();
            var user = new UserModel { Name = "ToDelete", Email = "delete@email.com" };
            controller.CreateUser(user);
            var result = controller.DeleteUser(1);
            Assert.IsType<NoContentResult>(result);
            var getResult = controller.GetUser(1);
            Assert.IsType<NotFoundResult>(getResult.Result);
        }

        [Fact]
        public void DeleteUser_ReturnsNotFound_WhenUserDoesNotExist()
        {
            var controller = new UserController();
            var result = controller.DeleteUser(999);
            Assert.IsType<NotFoundResult>(result);
        }
    }
}
